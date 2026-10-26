#!/usr/bin/env python3
"""Reclaim owned CE-spool directories of explicitly listed, ended ePIC jobs."""
import argparse
import gzip
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import time
import uuid

ROOT = '/var/lib/condor-ce/spool'
NAME = re.compile(r'(?:PanDA_Pilot-|epicprod-scratch-)([0-9]+)$')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if args.manifest.endswith('.gz'):
        with gzip.open(args.manifest, 'rt') as handle:
            manifest = json.load(handle)
    else:
        manifest = json.loads(Path(args.manifest).read_text())
    if (manifest['site'] != 'UM_GREX_PanDA_1' or manifest['uid'] != os.getuid()
            or not manifest.get('operator_decision') or time.time() - manifest['built_at'] > 1800):
        raise RuntimeError('The reclamation authority or manifest is invalid/stale')
    cutoff = manifest['cutoff']
    allowed = {int(row['pandaid']) for row in manifest['jobs']
               if row['endtime'] < cutoff and
               (row['status'] == 'finished' or
                row['status'] == 'failed' and row['piloterrorcode'] == 1098)}
    archive = Path(os.environ['OSG_WN_TMP']) / ('epicprod-reclaim-' + uuid.uuid4().hex)
    archive.mkdir(mode=0o700)
    journal = archive / 'journal.jsonl'
    journal.touch(mode=0o600)
    root = Path(ROOT).resolve()
    before = os.statvfs(str(root))
    count, bytes_found = 0, 0
    with journal.open('a') as log:
        for directory, dirs, files in os.walk(str(root), followlinks=False):
            if len(Path(directory).relative_to(root).parts) > 8:
                dirs[:] = []
                continue
            for name in list(dirs):
                match = NAME.fullmatch(name)
                if not match:
                    continue
                dirs.remove(name)  # Never scan inside a job's data.
                pandaid = int(match.group(1))
                path = Path(directory) / name
                if pandaid not in allowed or path.is_symlink():
                    continue
                info = path.lstat()
                if (info.st_uid != os.getuid() or not stat.S_ISDIR(info.st_mode)
                        or info.st_mtime > cutoff or root not in path.resolve().parents):
                    continue
                size = subprocess.run(['du', '-skx', str(path)], stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, universal_newlines=True, timeout=30)
                if size.returncode:
                    continue
                allocated = int(size.stdout.split()[0]) * 1024
                record = dict(pandaid=pandaid, path=str(path), allocated_bytes=allocated,
                              authority=manifest['operator_decision'], apply=args.apply,
                              at=time.time())
                log.write(json.dumps(record) + '\n'); log.flush(); os.fsync(log.fileno())
                if args.apply:
                    try:
                        shutil.rmtree(str(path))
                    except OSError as exc:
                        log.write(json.dumps(dict(record, outcome='error', error=str(exc))) + '\n')
                        log.flush()
                        continue
                    log.write(json.dumps(dict(record, outcome='removed')) + '\n'); log.flush()
                count += 1
                bytes_found += allocated
                print(json.dumps(record), flush=True)
    after = os.statvfs(str(root))
    print(json.dumps(dict(summary=True, directories=count, allocated_bytes=bytes_found,
                          available_before=before.f_bavail * before.f_frsize,
                          available_after=after.f_bavail * after.f_frsize,
                          journal=str(journal), apply=args.apply)), flush=True)


if __name__ == '__main__':
    main()
