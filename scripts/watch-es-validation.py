#!/usr/bin/env python3
"""Observe bounded ES validation tasks without changing their state."""
import argparse
import json
import os
from pathlib import Path
import sys
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task', type=int, action='append', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--timeout', type=int, default=7200)
    args = parser.parse_args()
    sys.path.insert(0, '/opt/swf-monitor/current/src')
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')
    import django
    django.setup()
    from django.db import connections
    args.output.parent.mkdir(parents=True, exist_ok=True)
    deadline, previous = time.monotonic() + args.timeout, None
    marks = ','.join(['%s'] * len(args.task))
    while time.monotonic() < deadline:
        with connections['panda'].cursor() as cursor:
            cursor.execute('SELECT jeditaskid,status FROM doma_panda.jedi_tasks '
                           'WHERE jeditaskid IN (' + marks + ') ORDER BY jeditaskid', args.task)
            tasks = cursor.fetchall()
            jobs = []
            for table in ('jobsactive4', 'jobsarchived4'):
                cursor.execute('SELECT jeditaskid,pandaid,jobstatus,jobsubstatus,nevents,'
                    'starttime,endtime,piloterrorcode FROM doma_panda.' + table
                    + ' WHERE jeditaskid IN (' + marks + ') ORDER BY pandaid', args.task)
                jobs.extend(cursor.fetchall())
        if len(tasks) != len(args.task):
            raise RuntimeError('A requested validation task is absent')
        value = json.dumps({'tasks': tasks, 'jobs': jobs}, default=str)
        if value != previous:
            record = dict(observed_at=time.time(), **json.loads(value))
            with args.output.open('a') as stream:
                stream.write(json.dumps(record) + '\n')
            print(json.dumps(record), flush=True)
            previous = value
        if all(status == 'done' for _, status in tasks):
            return 0
        if any(status in ('failed', 'broken', 'aborted', 'exhausted') for _, status in tasks):
            raise RuntimeError('A validation task ended without completing')
        connections.close_all()
        time.sleep(30)
    raise TimeoutError('The bounded validation observation timed out')


if __name__ == '__main__':
    raise SystemExit(main())
