#!/usr/bin/env python3
"""Set integer worker/job limit keys of one queue in a harvester's panda_queueconfig.json.

Run as root on the harvester host. Without --apply it prints the old and new
values only. With --apply it keeps a dated backup beside the file, validates
the result as JSON and replaces the file atomically, preserving its mode and
owner. The harvester re-reads the file within its update interval without a
restart (docs/CONTINUOUS_PRODUCTION.md, The limits).

    set-harvester-queue-limits.py UM_GREX_PanDA_1 nQueueLimitWorkerMax=50 \\
        nQueueLimitWorkerMin=25 --reason 'GREX CE spool: queued sandboxes' --apply
"""
import argparse
import datetime
import json
import os
from pathlib import Path
import shutil
import tempfile

KEYS = {'nQueueLimitWorker', 'nQueueLimitWorkerMax', 'nQueueLimitWorkerMin',
        'nQueueLimitWorkerRatio', 'maxWorkers', 'maxNewWorkersPerCycle',
        'nQueueLimitJob', 'nQueueLimitJobMax', 'nQueueLimitJobMin', 'nQueueLimitJobRatio'}


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('queue')
    parser.add_argument('settings', nargs='+', help='KEY=INTEGER')
    parser.add_argument('--config', type=Path, default=Path('/opt/harvester/etc/panda/panda_queueconfig.json'))
    parser.add_argument('--reason', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    changes = {}
    for setting in args.settings:
        key, _, value = setting.partition('=')
        if key not in KEYS or not value.isdigit():
            parser.error(f'not a known integer limit: {setting}')
        changes[key] = int(value)
    text = args.config.read_text()
    config = json.loads(text)
    entry = config.get(args.queue)
    if not isinstance(entry, dict):
        parser.error(f'{args.queue} has no entry in {args.config}')
    after = dict(entry, **changes)
    low, high = after.get('nQueueLimitWorkerMin'), after.get('nQueueLimitWorkerMax')
    if low is not None and high is not None and low > high:
        parser.error(f'nQueueLimitWorkerMin {low} exceeds nQueueLimitWorkerMax {high}')
    print(json.dumps({'queue': args.queue, 'reason': args.reason, 'apply': args.apply,
                      'before': {k: entry.get(k) for k in changes}, 'after': changes}))
    if not args.apply or all(entry.get(k) == v for k, v in changes.items()):
        return
    config[args.queue] = after
    content = json.dumps(config, indent=4) + '\n'
    json.loads(content)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = args.config.with_name(args.config.name + '.before-limits-' + stamp)
    shutil.copy2(args.config, backup)
    stat = args.config.stat()
    fd, temporary = tempfile.mkstemp(dir=args.config.parent, prefix=args.config.name + '.')
    try:
        with os.fdopen(fd, 'w') as out:
            out.write(content)
            out.flush()
            os.fsync(out.fileno())
            os.fchmod(out.fileno(), stat.st_mode & 0o7777)
            os.fchown(out.fileno(), stat.st_uid, stat.st_gid)
        os.replace(temporary, args.config)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print(f'installed {args.config}; backup {backup}')


if __name__ == '__main__':
    main()
