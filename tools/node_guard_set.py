#!/usr/bin/env python3
"""A person's decision on node guard records (site-canary docs/NODE_GUARD.md,
The record): clear, pin or open nodes by hand, as the Node guard page's
buttons do, recorded the same way (a ``node_guard_set`` action per node).

The page's buttons answer only on the monitor's internal face; this is the
same decision from the operating account, on an operator's word. Dry run
unless --apply.

    /opt/swf-monitor/current/.venv/bin/python node_guard_set.py QUEUE HOST... \\
        --action clear --note TEXT --by NAME [--apply]
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from es_credit_ended import deployed_environment  # noqa: E402

STATUS = {'clear': 'clear', 'unpin': 'clear', 'pin': 'pinned', 'open': 'black_hole'}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('queue')
    ap.add_argument('hosts', nargs='+')
    ap.add_argument('--action', required=True, choices=sorted(STATUS))
    ap.add_argument('--note', required=True)
    ap.add_argument('--by', required=True, help='who decided, and who carried it out')
    ap.add_argument('--apply', action='store_true')
    args = ap.parse_args()

    deployed_environment()
    from canary.store import nodes as node_record
    from canary.store.models import NodeState
    from monitor_app.epicprod_logging import log_epicprod_action

    known = {n.host: n.status for n in NodeState.objects.filter(queue__name=args.queue,
                                                                 host__in=args.hosts)}
    missing = [h for h in args.hosts if h not in known]
    if missing:
        sys.exit(f'ERROR: no node record on {args.queue} for {", ".join(missing)}')
    status = STATUS[args.action]
    failed = 0
    for host in args.hosts:
        if not args.apply:
            print(f'would {args.action} {args.queue} {host} ({known[host]} -> {status})')
            continue
        try:
            node = node_record.set_status(args.queue, host, status, username=args.by,
                                          reason=f'{args.action}: {args.note}')
        except Exception as exc:  # noqa: BLE001 - reported, counted, exit nonzero
            print(f'ERROR: {args.queue} {host}: {type(exc).__name__}: {exc}', file=sys.stderr)
            failed += 1
            continue
        log_epicprod_action(
            'ops-tool', 'node_guard_set', subject_type='panda_node',
            subject_key=f'{args.queue}/{host}', username=args.by, outcome=node.status,
            sublevel='normal', live_default=False,
            message=f'node guard {args.queue} {host}: {args.action} by {args.by} '
                    f'({args.note}); status {node.status}',
            action_taken=args.action, note=args.note, status=node.status)
        print(f'{args.queue} {host}: {known[host]} -> {node.status}')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
