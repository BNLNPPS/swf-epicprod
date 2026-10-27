#!/usr/bin/env python3
"""Set a feeder's task aside: pause it in PanDA through the operations agent
and record the operator's review on its PCS task, which releases the feeder's
supply (google_production.task_inventory skips a reviewed task; a paused task
no longer holds the queue). The task and its records stay for later study;
nothing is finished or killed. Dry run unless --apply.

Runs in the deployed monitor's environment on swf-testbed, on an operator's
word:

    /opt/swf-monitor/current/.venv/bin/python feeder_set_aside.py <jeditaskid> \
        --marker perlmutter_production --note '<why>' [--apply]
"""
import argparse
from datetime import datetime
import json
import os
import sys
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

PAUSABLE = ('registered', 'defined', 'assigning', 'ready', 'pending', 'scouting', 'running',
            'topreprocess', 'preprocessing', 'prepared', 'throttled')


def deployed_environment():
    for raw in open('/opt/swf-monitor/config/env/production.env').read().splitlines():
        raw = raw.strip().removeprefix('export ')
        if raw and not raw.startswith('#') and '=' in raw:
            key, value = raw.split('=', 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    sys.path.insert(0, '/opt/swf-monitor/current/src')
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')
    import django
    django.setup()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('jeditaskid', type=int)
    ap.add_argument('--marker', required=True, help='the feeder marker, e.g. perlmutter_production')
    ap.add_argument('--note', required=True, help='why it is set aside')
    ap.add_argument('--requested-by', default='wenaus')
    ap.add_argument('--apply', action='store_true')
    args = ap.parse_args()

    deployed_environment()
    from monitor_app.epicprod_logging import log_epicprod_action
    from monitor_app.panda.operations import queue_task_operation
    from monitor_app.panda.queries import get_task_operation_targets
    from pcs.models import ProdTask

    tasks = [t for t in ProdTask.objects.filter(overrides__has_key=args.marker)
             if t.panda_task_id == args.jeditaskid
             or t.panda_tasks.filter(jedi_task_id=args.jeditaskid).exists()]
    if len(tasks) != 1:
        sys.exit(f'ERROR: {len(tasks)} {args.marker} tasks carry PanDA task {args.jeditaskid}')
    task = tasks[0]
    entry = task.overrides[args.marker]
    if not isinstance(entry, dict):
        sys.exit(f'ERROR: PCS task {task.pk} has no {args.marker} record')
    current = get_task_operation_targets([args.jeditaskid])
    if not current:
        sys.exit(f'ERROR: no PanDA state for task {args.jeditaskid}')
    status = current[0]['status']
    stamp = datetime.now(ZoneInfo('America/New_York')).strftime('%Y-%m-%d %H:%M ET')
    review = f'{stamp}, {args.requested_by}: {args.note}'
    plan = {'jeditaskid': args.jeditaskid, 'prodtask': task.pk, 'sample': entry.get('sample'),
            'status': status, 'pause': status in PAUSABLE, 'reviewed_before': entry.get('reviewed'),
            'reviewed': review}
    print(json.dumps(plan))
    if not args.apply:
        return

    if status in PAUSABLE:
        record, created = queue_task_operation(
            task=current[0], operation='pause', requested_by=args.requested_by,
            source='feeder_set_aside', evidence={'marker': args.marker, 'note': args.note})
        print(json.dumps({'pause_operation': str(record.id), 'created': created}))
    task.overrides[args.marker] = dict(entry, reviewed=review)
    task.save(update_fields=['overrides'])
    log_epicprod_action(
        'ops-agent', 'feeder_set_aside', subject_type='panda_task', subject_key=str(args.jeditaskid),
        username=args.requested_by, outcome='ok', sublevel='normal', live_default=False,
        message=f"{args.marker} task {args.jeditaskid} ({entry.get('sample')}) set aside: {args.note}",
        jeditaskid=args.jeditaskid, prodtask=task.pk, marker=args.marker, status=status)


if __name__ == '__main__':
    main()
