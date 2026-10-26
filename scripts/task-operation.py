#!/usr/bin/env python3
"""Queue a PanDA task operation through the production operations agent, as
the task page's controls do (swf-monitor monitor_app/panda/operations.py):
pause, resume, retry_failures or finish. The agent holds the credential and
verifies the outcome; this records who asked and why.

Runs in the deployed monitor's environment on swf-testbed:

    /opt/swf-monitor/current/.venv/bin/python task-operation.py --task 41249 --operation finish \\
        --reason 'canary sized beyond one allocation; no second job wanted'
"""
import argparse
import json
import os
from pathlib import Path
import sys


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--task', type=int, required=True)
    ap.add_argument('--operation', required=True, choices=('pause', 'resume', 'retry_failures', 'finish'))
    ap.add_argument('--reason', required=True)
    ap.add_argument('--requested-by', default='wenaus')
    args = ap.parse_args()
    for raw in Path('/opt/swf-monitor/config/env/production.env').read_text().splitlines():
        raw = raw.strip().removeprefix('export ')
        if raw and not raw.startswith('#') and '=' in raw:
            key, value = raw.split('=', 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    sys.path.insert(0, '/opt/swf-monitor/current/src')
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')
    import django
    django.setup()
    from monitor_app.panda.operations import queue_task_operation
    from monitor_app.panda.queries import get_task_operation_targets
    targets = get_task_operation_targets([args.task])
    if not targets:
        sys.exit(f'ERROR: task {args.task} not found')
    record, created = queue_task_operation(task=targets[0], operation=args.operation,
                                           requested_by=args.requested_by, source='manual',
                                           evidence={'reason': args.reason})
    print(json.dumps({'task': args.task, 'status': targets[0].get('status'), 'operation': args.operation,
                      'operation_id': str(record.id), 'queued': created}))


if __name__ == '__main__':
    main()
