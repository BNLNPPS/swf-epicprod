#!/usr/bin/env python3
"""Set one queue's automatic front feed and record the exact change.

``on`` lets the front feed the queue; ``off`` stops it; ``exempt`` stops it
because the queue's work comes from its own feeder, which is not a fault.
Every change carries its reason, who made it and when, written where every
display reads them (docs/CONTINUOUS_PRODUCTION.md, Set points and caps):
``front.queue.<queue>.feed_off_reason`` for off, ``.feed_exempt`` for
exempt, ``.feed_set_by`` and ``.feed_set_at`` for any change.

Run in the monitor Django environment with the testbed venv and ~/.env.
"""
import argparse
import os

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django
django.setup()

from django.db import transaction
from django.utils import timezone
from monitor_app.epicprod_logging import log_epicprod_action
from monitor_app.models import SysConfig
from swf_epicprod import queues


def mode(config, base):
    """The switch as it stands: on, exempt or off."""
    if config.get(f'{base}.feed') is True:
        return 'on'
    return 'exempt' if config.get(f'{base}.feed_exempt') else 'off'


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('queue')
    parser.add_argument('feed', choices=('on', 'off', 'exempt'))
    parser.add_argument('--by', required=True)
    parser.add_argument('--reason', required=True)
    parser.add_argument('--decision-ref', required=True)
    args = parser.parse_args()
    if args.queue not in queues.regulated():
        parser.error('queue is not regulated by the production front')
    if not args.reason.strip():
        parser.error('a reason is required')
    base = f'front.queue.{args.queue}'
    with transaction.atomic():
        config = SysConfig.objects.select_for_update().get(pk=1).config_data
        before = mode(config, base)
        reason_key = {'off': f'{base}.feed_off_reason', 'exempt': f'{base}.feed_exempt'}.get(args.feed)
        if before == args.feed and (reason_key is None or config.get(reason_key) == args.reason):
            print(f'{args.queue} feed is already {args.feed}')
            return
        updates = {
            f'{base}.feed': args.feed == 'on',
            f'{base}.feed_off_reason': args.reason if args.feed == 'off' else '',
            f'{base}.feed_exempt': args.reason if args.feed == 'exempt' else '',
            f'{base}.feed_set_by': args.by,
            f'{base}.feed_set_at': timezone.now().isoformat(),
        }
        if args.feed != 'off':
            # An automatic return applies to an off feed only (feed_guard.py).
            updates[f'{base}.feed_auto_on'] = ''
        previous = {key: config.get(key) for key in updates}
        SysConfig.update_config(updates, username=args.by)
        record = log_epicprod_action(
            'front-control', 'sysconfig_edit', username=args.by,
            sublevel='high', live_default=True,
            subject_type='panda_queue', subject_key=args.queue,
            keys=sorted(updates), previous=previous, updated=updates,
            reason=args.reason, decision_ref=args.decision_ref,
            summary=f'{args.queue} automatic feed {before} -> {args.feed}: {args.reason}')
        if record is None:
            raise RuntimeError('Configuration change audit failed; change rolled back')
    print(f'{args.queue} feed: {before} -> {args.feed}; audit record {record}')


if __name__ == '__main__':
    main()
