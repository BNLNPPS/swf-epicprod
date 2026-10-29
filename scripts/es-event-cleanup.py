#!/usr/bin/env python3
"""Remove the Event Service range rows (JEDI_Events) of tasks done or aborted
more than three days ago (swf_epicprod/event_cleanup.py;
docs/ES_EVENT_CLEANUP.md).

    es-event-cleanup.py            delete, record one es_event_cleanup action
    es-event-cleanup.py --dry-run  list the tasks and their rows; delete nothing

Run in the monitor Django environment with ~/.env. Daily from cron; a run
already in progress holds the lock and a second one exits.
"""
import argparse
import fcntl
import json
import os
import sys

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402
django.setup()

from swf_epicprod.event_cleanup import run  # noqa: E402

LOCK = os.path.expanduser('~/.es-event-cleanup.lock')


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()
    with open(LOCK, 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print('another cleanup is running')
            return 0
        record = run(dry_run=args.dry_run)
    print(json.dumps({k: v for k, v in record.items() if k != 'tasks'}))
    for t in record['tasks']:
        print(f"{t['task']} {t['status']} ended {t['ended']}: {t['rows']:,} rows")
    return 1 if record['error'] else 0


if __name__ == '__main__':
    sys.exit(main())
