#!/usr/bin/env python3
"""Supervise a stage; preserve fatal evidence and stop a stalled crash handler.

See docs/EPICPROD_PAYLOAD.md. Only the process group started here is
terminated, after a fatal signal and fresh prmon samples proving no progress.
"""
import argparse
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone

SIGNALS = {6: 'SIGABRT', 7: 'SIGBUS', 8: 'SIGFPE', 11: 'SIGSEGV'}
COUNTERS = ('utime', 'stime', 'rchar', 'wchar', 'read_bytes', 'write_bytes')
TAIL_BYTES = 65536


def fatal_signal(text):
    """Accept explicit signal-handler records, never geometry warnings alone."""
    matches = re.findall(r'Handle signal:\s*(6|7|8|11)\s*\[(SIG[A-Z]+)\]', text)
    for number, name in reversed(matches):
        if SIGNALS.get(int(number)) == name:
            return {'signal': int(number), 'signal_name': name}
    match = re.search(r'\*\*\* Break \*\*\*\s+(segmentation violation|abort)', text)
    if match:
        number = 11 if match[1] == 'segmentation violation' else 6
        return {'signal': number, 'signal_name': SIGNALS[number]}
    return None


def tail(path):
    try:
        with open(path, 'rb') as handle:
            handle.seek(max(0, os.fstat(handle.fileno()).st_size - TAIL_BYTES))
            return handle.read(TAIL_BYTES).decode(errors='replace')
    except FileNotFoundError:
        return ''


def sample(path):
    """Read the last complete sample; missing/stale measurements never kill."""
    try:
        with open(path) as handle:
            header = handle.readline().split()
        lines = tail(path).splitlines()
        if not lines:
            return None
        fields = lines[-1].split()
        if len(fields) != len(header):
            return None
        row = dict(zip(header, fields))
        return int(row['Time']), tuple(int(row[k]) for k in COUNTERS)
    except FileNotFoundError:
        return None
    except (KeyError, ValueError):
        return None


def write_json(path, value):
    temp = str(path) + '.writing'
    Path(temp).write_text(json.dumps(value))
    os.replace(temp, path)


def diagnostics(pgid):
    """Bounded process identity and wait states, without argv or environment."""
    out = []
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            stat = (proc / 'stat').read_text()
            fields = stat[stat.rfind(')') + 2:].split()
            if int(fields[2]) != pgid:
                continue
            out.append({'pid': int(proc.name), 'comm': (proc / 'comm').read_text().strip(),
                        'state': fields[0], 'wchan': (proc / 'wchan').read_text().strip()})
        except (FileNotFoundError, ProcessLookupError):
            continue
        except (OSError, ValueError) as exc:
            print(f'fatal watch: process {proc.name}: {exc}', file=sys.stderr)
        if len(out) == 64:
            break
    return out


def publish(args, evidence):
    write_json(args.evidence, evidence)
    # The parent shell is waiting for this stage. Update its last report
    # and the pilot digest now, so a later external kill retains the signal.
    for path in (args.report, args.job_report):
        try:
            body = json.loads(Path(path).read_text())
            report = body.get('payload', body)
            report['fatal'] = evidence
            metrics = report.setdefault('jobMetrics', {})
            metrics.update(payloadFatalSignal=evidence['signal'],
                           payloadFatalStage=args.stage)
            if evidence.get('terminated'):
                metrics['payloadFatalStalled'] = 1
            if 'payload' in body:
                body['jobMetrics'] = metrics
            write_json(path, body)
        except (OSError, ValueError, TypeError) as exc:
            print(f'fatal watch: cannot refresh {path}: {exc}', file=sys.stderr)


def supervise(args):
    proc = subprocess.Popen(args.command, start_new_session=True)
    evidence = None
    baseline = None
    last_sample = None
    def stop_group(signum):
        try:
            os.killpg(proc.pid, signum)
        except ProcessLookupError:
            return
    def interrupted(signum, _frame):
        stop_group(signum)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            stop_group(signal.SIGKILL)
        finally:
            stop_group(signal.SIGKILL)
        raise SystemExit(128 + signum)
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, interrupted)
    try:
        while proc.poll() is None:
            text = tail(args.log)
            found = fatal_signal(text)
            if found and evidence is None:
                evidence = dict(found, stage=args.stage, source='stage_log',
                                observed_at=datetime.now(timezone.utc).isoformat(),
                                log=Path(args.log).name, tail=text[-16000:])
                publish(args, evidence)
            current = sample(args.series) if evidence else None
            if current is None or not 0 <= time.time() - current[0] <= 90:
                baseline = last_sample = None
            else:
                # The full interval must have consecutive, fresh samples;
                # a stopped monitor is not proof of a stopped payload.
                if (baseline is None or current[1] != baseline[1]
                        or (last_sample and current[0] - last_sample[0] > 90)):
                    baseline = current
                last_sample = current
                if current[0] - baseline[0] >= args.idle_seconds:
                    evidence.update(terminated=True, idle_s=current[0] - baseline[0],
                                    counters=dict(zip(COUNTERS, current[1])),
                                    processes=diagnostics(proc.pid))
                    publish(args, evidence)
                    print(f"fatal watch: {evidence['signal_name']} in {args.stage}; "
                          f"no CPU or file I/O progress for {evidence['idle_s']}s; terminating stage",
                          flush=True)
                    stop_group(signal.SIGTERM)
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        stop_group(signal.SIGKILL)
                        proc.wait(timeout=10)
                    # A surviving descendant can hold the output pipe open.
                    stop_group(signal.SIGKILL)
                    return 128 + evidence['signal']
            time.sleep(args.interval)
        rc = proc.returncode
        return 128 - rc if rc < 0 else rc
    finally:
        if proc.poll() is None:
            stop_group(signal.SIGKILL)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--stage', required=True)
    parser.add_argument('--log', required=True)
    parser.add_argument('--series', required=True)
    parser.add_argument('--evidence', required=True)
    parser.add_argument('--report', default='payload-report.json')
    parser.add_argument('--job-report', default='jobReport.json')
    parser.add_argument('--idle-seconds', type=float, default=120)
    parser.add_argument('--interval', type=float, default=5)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.command[:1] == ['--']:
        args.command.pop(0)
    if not args.command or args.idle_seconds <= 0 or args.interval <= 0:
        parser.error('a command and positive timing bounds are required')
    return supervise(args)


if __name__ == '__main__':
    sys.exit(main())
