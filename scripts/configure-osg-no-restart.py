#!/usr/bin/env python3
"""Install the OSG pull pilot's eviction policy for future submissions only.

Run on osgsub01 with render-native-production.py beside this script. A plan
prints the exact diff and source hash. --apply requires that reviewed hash,
keeps a dated backup, and atomically replaces only the submit description.
No queued or running Condor ad, worker record or PanDA job is changed here.
"""
import argparse
import datetime
import difflib
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--template', type=Path, default=Path(
        '/var/data/atlpan/harvester_common/submit_pilot2_pull_bnl_osg.sdf'))
    parser.add_argument('--expected-sha256')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if args.apply and not args.expected_sha256:
        parser.error('--apply requires --expected-sha256 from the reviewed plan')
    spec = importlib.util.spec_from_file_location('native_renderer',
        Path(__file__).with_name('render-native-production.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    path = args.template
    with path.with_name(path.name + '.site-exclusion.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        before_bytes = path.read_bytes()
        before = before_bytes.decode()
        if ('/epic-native-pilot.sh' not in before
                or '--harvester-submit-mode PULL' not in before):
            raise ValueError('expected the production native pull pilot')
        digest = hashlib.sha256(before_bytes).hexdigest()
        if args.expected_sha256 and args.expected_sha256 != digest:
            raise RuntimeError('template changed since review; make a fresh plan')
        after = module.no_restart(before)
        result = {'template': str(path), 'before_sha256': digest,
                  'after_sha256': hashlib.sha256(after.encode()).hexdigest(),
                  'changed': before != after, 'applied': False,
                  'existing_workers_edited': 0}
        if args.apply and before != after:
            stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            backup = path.with_name(path.name + '.before-no-restart-' + stamp)
            shutil.copy2(str(path), str(backup))
            if backup.read_bytes() != before_bytes:
                raise RuntimeError('template changed while making backup')
            info = path.stat()
            fd, temporary = tempfile.mkstemp(dir=str(path.parent), suffix='.sdf')
            try:
                with os.fdopen(fd, 'w') as out:
                    out.write(after)
                    out.flush()
                    os.fsync(out.fileno())
                    os.fchmod(out.fileno(), info.st_mode & 0o7777)
                    os.fchown(out.fileno(), info.st_uid, info.st_gid)
                if path.read_bytes() != before_bytes:
                    raise RuntimeError('template changed concurrently; make a fresh plan')
                os.replace(temporary, str(path))
                if path.read_text() != after:
                    raise RuntimeError('installed template does not match the plan')
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            result.update(applied=True, backup=str(backup), at=stamp)
        elif not args.apply:
            print(''.join(difflib.unified_diff(before.splitlines(True),
                after.splitlines(True), fromfile=str(path), tofile=str(path))), end='')
        print(json.dumps(result))


if __name__ == '__main__':
    main()
