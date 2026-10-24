#!/usr/bin/env python3
"""Set one queue's automatic front feed and record the exact change.

Run in the monitor Django environment with the testbed venv and ~/.env.
"""
import argparse
import os

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django
django.setup()

from django.db import transaction
from monitor_app.epicprod_logging import log_epicprod_action
from monitor_app.models import SysConfig
from swf_epicprod import queues


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('queue')
    parser.add_argument('feed', choices=('on', 'off'))
    parser.add_argument('--by', required=True)
    parser.add_argument('--reason', required=True)
    parser.add_argument('--decision-ref', required=True)
    args = parser.parse_args()
    if args.queue not in queues.regulated():
        parser.error('queue is not regulated by the production front')
    key = f'front.queue.{args.queue}.feed'
    enabled = args.feed == 'on'
    with transaction.atomic():
        config = SysConfig.objects.select_for_update().get(pk=1)
        previous = config.config_data.get(key)
        if previous is enabled:
            print(f'{key} is already {enabled}')
            return
        SysConfig.update_config({key: enabled}, username=args.by)
        record = log_epicprod_action(
            'front-control', 'sysconfig_edit', username=args.by,
            sublevel='high', live_default=True,
            subject_type='panda_queue', subject_key=args.queue,
            keys=[key], previous={key: previous}, updated={key: enabled},
            reason=args.reason, decision_ref=args.decision_ref,
            summary=f'{args.queue} automatic feed {previous} -> {enabled}: {args.reason}')
        if record is None:
            raise RuntimeError('Configuration change audit failed; change rolled back')
    print(f'{key}: {previous} -> {enabled}; audit record {record}')


if __name__ == '__main__':
    main()
