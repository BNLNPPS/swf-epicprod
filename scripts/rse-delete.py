#!/usr/bin/env python3
"""rse-delete.py — remove datasets from one disk RSE where tape holds a
complete copy (docs/STORAGE.md, Removing datasets from an RSE; logic in
swf_epicprod/rse_deletion.py).

Previews by default: every dataset matching the pattern, its files and
bytes, its tape copy, and whether it would be removed or why not.
``--execute`` expires the RSE's rule on each eligible dataset after the
RSE name is typed back (``--yes`` skips the prompt). The removal is
immediate and cannot be undone.

The catalog is JLab Rucio as ``eicprod`` by a PanDA-server token, else
the proxy in EVGEN_X509_PROXY; ``--rucio-cfg`` uses the caller's own
rucio.cfg account instead. A dataset whose production task is active is
kept; task state is read through the monitor's Django (PCS and PanDA),
found at SWF_MONITOR_SRC or the sibling swf-monitor checkout. Where that
cannot be set up, as off the production host, every dataset is kept for
want of it and the preview says so. The same removal is offered on the monitor's
storage page, run by the production operations agent.

Usage::

    python scripts/rse-delete.py --rse BNL-XRD --pattern '/RECO/26.03.0/*'
    python scripts/rse-delete.py --rse BNL-XRD --pattern '/RECO/26.03.0/*' --execute
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from swf_epicprod import rse_deletion  # noqa: E402


def _client(args):
    if args.rucio_cfg:
        from rucio.client import Client
        return Client()
    from swf_epicprod.jlab_auth import rucio_client
    return rucio_client(proxy=os.environ.get('EVGEN_X509_PROXY'), timeout=60)


def _task_reader():
    """The storage pass's task-state reader through the monitor's Django,
    or None when it cannot be set up here."""
    src = os.environ.get('SWF_MONITOR_SRC') or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), '..', '..', 'swf-monitor', 'src')
    try:
        sys.path.insert(0, src)
        os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')
        import django
        django.setup()
        from swf_epicprod.analytics.storage import _task_states
        return _task_states
    except Exception as e:                                    # noqa: BLE001
        print(f'task state not readable here ({type(e).__name__}: {e}); '
              'every dataset is kept for want of it', file=sys.stderr)
        return None


def _tb(n):
    return f'{n / 1e12:.2f} TB'


def _print_plan(p):
    for i in p['datasets']:
        mark = 'REMOVE' if i['eligible'] else 'keep  '
        why = i['tape'] if i['eligible'] else i['reason']
        print(f"{mark} {i['name']}  {i['files']} files {_tb(i['bytes'])}  {why}")
    t = p['totals']
    print(f"\n{t['datasets']} datasets match {p['pattern']} on {p['rse']}: "
          f"{t['eligible']} eligible ({t['eligible_files']} files, "
          f"{_tb(t['eligible_bytes'])}), {t['refused']} refused.")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--rse', required=True,
                        help='disk RSE: ' + ', '.join(rse_deletion.DISK_RSES))
    parser.add_argument('--pattern', required=True,
                        help="dataset name pattern, e.g. '/RECO/26.03.0/*'")
    parser.add_argument('--execute', action='store_true',
                        help='expire the rules (immediate, irreversible)')
    parser.add_argument('--yes', action='store_true',
                        help='with --execute, skip the typed confirmation')
    parser.add_argument('--json', action='store_true', help='print JSON')
    parser.add_argument('--rucio-cfg', action='store_true',
                        help="use the caller's rucio.cfg account")
    args = parser.parse_args()

    try:
        rse_deletion.validate(args.rse, args.pattern)
    except rse_deletion.RseDeletionError as e:
        sys.exit(f'refused: {e}')
    client = _client(args)
    task_states = _task_reader()
    try:
        p = rse_deletion.plan(client, args.rse, args.pattern, task_states)
    except rse_deletion.RseDeletionError as e:
        sys.exit(f'refused: {e}')
    if not args.execute:
        print(json.dumps(p, indent=1) if args.json else '', end='')
        if not args.json:
            _print_plan(p)
            print('Preview only; add --execute to remove.')
        return 0

    names = [i['name'] for i in p['datasets'] if i['eligible']]
    if not args.json:
        _print_plan(p)
    if not names:
        print('Nothing eligible; nothing removed.')
        return 0
    if not args.yes:
        print(f'\nWARNING: this removes {len(names)} datasets from {args.rse} '
              'IMMEDIATELY and cannot be undone.')
        if input(f'Type {args.rse} to proceed: ').strip() != args.rse:
            print('Not confirmed; nothing removed.')
            return 1
    result = rse_deletion.execute(client, args.rse, args.pattern, names, task_states)
    if args.json:
        print(json.dumps(result, indent=1))
    else:
        for o in result['datasets']:
            if o['outcome'] != 'removed':
                print(f"{o['outcome']}: {o['name']}  {o['reason']}")
        t = result['totals']
        print(f"Removed {t['removed']} of {t['requested']} "
              f"({t['removed_files']} files, {_tb(t['removed_bytes'])}); "
              f"{t['skipped']} skipped, {t['failed']} failed.")
    return 2 if result['totals']['failed'] else 0


if __name__ == '__main__':
    sys.exit(main())
