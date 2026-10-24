#!/usr/bin/env python
"""Show or set a production queue's status in the queue registry
(docs/QUEUE_REGISTRY.md).

    queue-status.py                          the record, one line per queue
    queue-status.py QUEUE STATUS --reason '...' --by 'Name'
                                             set one queue's status
    queue-status.py --adopt [--strip-room]   write the derived record now;
                                             --strip-room also drops the room's
                                             own queue lists from comp.sites
    queue-status.py --jedi-pools             the JEDI throttler POOLS the
                                             record implies

Runs in the monitor's Django environment: the testbed venv and ~/.env.
"""
import argparse
import os
import sys

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402

django.setup()

from swf_epicprod import queues as registry  # noqa: E402


def show():
    for e in registry.entries():
        flags = ', '.join(x for x in (e['site'] and f"site {e['site']}",
                                      e['pool'] and f"pool {e['pool']}",
                                      e['front'] and 'front') if x)
        when = f"  [{e['since']} {e['by']}]" if e['since'] else ''
        print(f"{e['queue']:<28} {e['status']:<13} {flags:<28} {e['reason']}{when}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('queue', nargs='?')
    ap.add_argument('status', nargs='?', choices=registry.STATUSES)
    ap.add_argument('--reason', default='')
    ap.add_argument('--by')
    ap.add_argument('--site')
    ap.add_argument('--pool')
    ap.add_argument('--front', choices=('yes', 'no'))
    ap.add_argument('--adopt', action='store_true')
    ap.add_argument('--strip-room', action='store_true')
    ap.add_argument('--jedi-pools', action='store_true')
    args = ap.parse_args()
    if args.jedi_pools:
        print(registry.jedi_pools())
        return
    if args.adopt:
        if not args.by:
            ap.error('--adopt needs --by')
        _, adopted, stripped = registry.adopt(args.by, strip_room=args.strip_room)
        print(f"adopted: {'yes' if adopted else 'already'}; room queue lists stripped: "
              f"{'yes' if stripped else 'no'}")
        show()
        return
    if args.queue:
        if not (args.status and args.by):
            ap.error('setting a status needs QUEUE STATUS --by NAME')
        if args.status not in ('production',) and not args.reason:
            ap.error(f'{args.status} needs --reason')
        fields = {k: v for k, v in (('site', args.site), ('pool', args.pool)) if v}
        if args.front:
            fields['front'] = args.front == 'yes'
        before = registry.set_status(args.queue, args.status, reason=args.reason,
                                     by=args.by, **fields)
        print(f'{args.queue}: {before} -> {args.status}')
        print('Copies outside the monitor follow by their installers '
              '(docs/QUEUE_REGISTRY.md, The copies outside the monitor); '
              f'the throttler POOLS the record implies: {registry.jedi_pools()}')
    show()


if __name__ == '__main__':
    sys.exit(main())
