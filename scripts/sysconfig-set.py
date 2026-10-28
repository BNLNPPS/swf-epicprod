#!/usr/bin/env python3
"""Set one SysConfig key and record the exact change, who and why.

For a production switch with no control of its own (a feeder's
``*.enabled``): the previous and new values, the reason and the decision
reference go to the action stream as one ``sysconfig_edit`` record, in the
same transaction as the change. The value is read as JSON (true, 300,
"text"); anything that is not JSON is taken as a string. A queue's front
feed has its own control, scripts/front-feed.py.

Run in the monitor Django environment with the testbed venv and ~/.env.
"""
import argparse
import json
import os

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django
django.setup()

from django.db import transaction
from monitor_app.epicprod_logging import log_epicprod_action
from monitor_app.models import SysConfig


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('key')
    parser.add_argument('value')
    parser.add_argument('--by', required=True)
    parser.add_argument('--reason', required=True)
    parser.add_argument('--decision-ref', required=True)
    args = parser.parse_args()
    if args.key.startswith('front.queue.') and args.key.rsplit('.', 1)[1].startswith('feed'):
        parser.error('a front feed is set with scripts/front-feed.py')
    if not args.reason.strip():
        parser.error('a reason is required')
    try:
        value = json.loads(args.value)
    except ValueError:
        value = args.value
    with transaction.atomic():
        config = SysConfig.objects.select_for_update().get(pk=1).config_data
        if args.key not in config:
            parser.error(f'{args.key} is not a SysConfig key; a new key is seeded by its reader')
        before = config[args.key]
        if before == value:
            print(f'{args.key} is already {json.dumps(value)}')
            return
        SysConfig.update_config({args.key: value}, username=args.by)
        record = log_epicprod_action(
            'sysconfig-set', 'sysconfig_edit', username=args.by,
            subject_type='sysconfig', subject_key=args.key,
            keys=[args.key], previous={args.key: before}, updated={args.key: value},
            reason=args.reason, decision_ref=args.decision_ref,
            summary=f'{args.key} {json.dumps(before)} -> {json.dumps(value)}: {args.reason}')
        if record is None:
            raise RuntimeError('Configuration change audit failed; change rolled back')
    print(f'{args.key}: {json.dumps(before)} -> {json.dumps(value)}; audit record {record}')


if __name__ == '__main__':
    main()
