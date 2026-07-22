"""One-off fan-out consolidation under the output-ownership rule.

EPICPROD_DATA_LINEAGE.md § Output ownership: one produced DID has one
owning record — the edition (or stage-matching past row) holds the full
``outputs`` entry; every other holder keeps a light ``output_refs``
entry. The writers now enforce this; this script settles the existing
fan-out by running the same idempotent consolidation
(``pcs.services.consolidate_output_ownership``) across all campaigns.

Run under the venv with the swf-monitor project on the path:

    cd <swf-monitor>/src && source <venv>/bin/activate && source ~/.env
    python <swf-epicprod>/scripts/migrate_output_ownership.py [--apply]

Dry-run by default; --apply writes.
"""

import argparse
import os
import sys

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402

django.setup()

from pcs.models import Campaign, ProdTask  # noqa: E402
from pcs.services import (_output_owner_names,  # noqa: E402
                          consolidate_output_ownership)


def dry_run_counts(campaign):
    owners = _output_owner_names(campaign)
    holders = 0
    would_move = 0
    for task in ProdTask.objects.filter(campaign=campaign):
        for entry in (task.overrides or {}).get('outputs') or []:
            did = entry.get('did') or ''
            if not did:
                continue
            holders += 1
            owner = owners.get(did) or task.name
            if owner != task.name:
                would_move += 1
    return holders, would_move


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true',
                        help='write the change (default: dry-run report)')
    args = parser.parse_args()

    total_moved = 0
    for campaign in Campaign.objects.order_by('name'):
        if args.apply:
            result = consolidate_output_ownership(campaign)
            moved = result['moved_to_refs']
            if moved or result['tasks_touched']:
                print(f'{campaign.name}: moved {moved} records to refs '
                      f'({result["tasks_touched"]} tasks touched)')
            total_moved += moved
        else:
            holders, would_move = dry_run_counts(campaign)
            if holders:
                print(f'{campaign.name}: {holders} output records held; '
                      f'{would_move} would become refs')
            total_moved += would_move
    verb = 'moved' if args.apply else 'would move'
    print(f'Total: {verb} {total_moved} records to refs.')
    if not args.apply:
        print('Dry run — rerun with --apply to consolidate.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
