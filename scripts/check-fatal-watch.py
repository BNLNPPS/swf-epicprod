#!/usr/bin/env python3
"""Bounded subprocess checks for the fatal-stage watchdog; no physics jobs."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile

PAYLOAD = Path(__file__).resolve().parents[1] / 'swf_epicprod' / 'payload'
sys.path.insert(0, str(PAYLOAD))
from fatal_watch import fatal_signal

CHILD = '''import pathlib,sys,time
mode=sys.argv[1]
if mode != 'quiet':
 print('[FATAL] (SignalHandler) Handle signal: 6 [SIGABRT]',flush=True)
for i in range(18):
 t=int(time.time()) if mode != 'stale' else int(time.time())-1000
 cpu=i if mode == 'progress' else 1
 with open('series.txt','a') as f: f.write(f'{t} {cpu} 0 0 0 0 0\\n')
 time.sleep(.2)
'''


def main():
    assert fatal_signal('GeomNav0003 Event Must Be Aborted') is None
    assert fatal_signal('Handle signal: 6 [SIGSEGV]') is None
    for mode in ('stalled', 'quiet', 'progress', 'stale'):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'child.py').write_text(CHILD)
            (root / 'series.txt').write_text('Time utime stime rchar wchar read_bytes write_bytes\n')
            (root / 'payload-report.json').write_text('{"jobMetrics": {}}')
            (root / 'jobReport.json').write_text('{"payload": {}, "jobMetrics": {}}')
            with (root / 'stage.log').open('w') as log:
                p = subprocess.run([sys.executable, str(PAYLOAD / 'fatal_watch.py'),
                                    '--stage', 'simulation', '--log', 'stage.log',
                                    '--series', 'series.txt', '--evidence', 'fatal.json',
                                    '--idle-seconds', '1', '--interval', '.1', '--',
                                    sys.executable, 'child.py', mode], cwd=tmp,
                                   stdout=log, stderr=subprocess.PIPE, text=True, timeout=10)
            assert p.returncode == (134 if mode == 'stalled' else 0), (mode, p.returncode, p.stderr)
            evidence = json.loads((root / 'fatal.json').read_text()) if (root / 'fatal.json').exists() else {}
            assert bool(evidence.get('terminated')) == (mode == 'stalled'), (mode, evidence)
            if mode == 'stalled':
                assert evidence['processes'] and evidence['tail']
                assert json.loads((root / 'jobReport.json').read_text())['jobMetrics']['payloadFatalSignal'] == 6
            print(f'{mode}: passed')


if __name__ == '__main__':
    main()
