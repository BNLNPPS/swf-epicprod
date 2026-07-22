"""One-off merge of patch-level Campaign rows into family campaigns.

docs/CAMPAIGN_FAMILY.md: the campaign is the family (first two version
fields); the third field is the software patch level, recorded on
datasets and tasks. This migration renames the lowest row of each
family group to the family name, re-points the two Campaign FKs
(Dataset.campaign, ProdTask.campaign) from sibling edition rows,
merges the data blobs, and deletes the emptied rows.

Data merge: ``arrivals`` keeps the newest block across members;
per-row stage-keyed ``past_summary`` blocks convert to
``past_summary_editions[edition][stage]`` with the stage rollup
rebuilt; ``rucio_unmatched`` refreshes on the next sync and is not
carried. Lifecycle precedence: current > last > future > past.

Run under the venv with the swf-monitor project on the path:

    cd <swf-monitor>/src && source <venv>/bin/activate && source ~/.env
    python <swf-epicprod>/scripts/migrate_campaign_families.py [--apply]

Dry-run by default; --apply writes.
"""

import argparse
import os
import sys
from collections import defaultdict

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402

django.setup()

from django.db import transaction  # noqa: E402

from pcs.models import Campaign, Dataset, ProdTask  # noqa: E402
from pcs.name_tokens import campaign_family  # noqa: E402

LIFECYCLE_PRECEDENCE = {'current': 0, 'last': 1, 'future': 2, 'past': 3}


def _version_key(name):
    try:
        return tuple(int(p) for p in str(name).split('.'))
    except ValueError:
        return (999,)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true',
                        help='write the change (default: dry-run report)')
    args = parser.parse_args()

    groups = defaultdict(list)
    for campaign in Campaign.objects.order_by('name'):
        groups[campaign_family(campaign.name)].append(campaign)

    plan = []
    for family, members in sorted(groups.items()):
        members.sort(key=lambda c: _version_key(c.name))
        keeper, siblings = members[0], members[1:]
        lifecycle = min((c.lifecycle for c in members),
                        key=lambda v: LIFECYCLE_PRECEDENCE.get(v, 9))
        moves = sum(
            Dataset.objects.filter(campaign=c).count()
            + ProdTask.objects.filter(campaign=c).count()
            for c in siblings)
        plan.append((family, keeper, siblings, lifecycle, moves))
        member_names = ', '.join(f'{c.name}({c.lifecycle})' for c in members)
        print(f'{family:8} <- {member_names:60} '
              f'lifecycle={lifecycle} rows-to-move={moves}')
    print(f'{len(Campaign.objects.all())} campaign rows -> '
          f'{len(groups)} families')

    if not args.apply:
        print('Dry run — rerun with --apply to migrate.')
        return 0

    with transaction.atomic():
        for family, keeper, siblings, lifecycle, _moves in plan:
            data = dict(keeper.data or {})
            editions = dict(data.pop('past_summary_editions', None) or {})
            arrivals_candidates = []
            for member in [keeper] + siblings:
                mdata = member.data or {}
                if isinstance(mdata.get('arrivals'), dict):
                    arrivals_candidates.append(mdata['arrivals'])
                summary = mdata.get('past_summary')
                if isinstance(summary, dict) and summary and \
                        'file_count' not in summary:
                    # Old shape: stage-keyed totals on a patch-level row.
                    if member.name != family:
                        editions.setdefault(member.name, summary)
                    elif not editions:
                        editions.setdefault(member.name, summary)
            stage_rollup = {}
            for edition_totals in editions.values():
                for stage, totals in (edition_totals or {}).items():
                    if not isinstance(totals, dict):
                        continue
                    agg = stage_rollup.setdefault(
                        stage, {'file_count': 0, 'data_size_bytes': 0})
                    agg['file_count'] += totals.get('file_count') or 0
                    agg['data_size_bytes'] += totals.get('data_size_bytes') or 0
            if editions:
                data['past_summary_editions'] = editions
                data['past_summary'] = stage_rollup
            if arrivals_candidates:
                data['arrivals'] = max(
                    arrivals_candidates,
                    key=lambda a: str(a.get('last_arrival_at') or ''))
            data.pop('rucio_unmatched', None)

            for sibling in siblings:
                Dataset.objects.filter(campaign=sibling).update(campaign=keeper)
                ProdTask.objects.filter(campaign=sibling).update(campaign=keeper)
            keeper.name = family
            keeper.lifecycle = lifecycle
            keeper.data = data
            keeper.save(update_fields=['name', 'lifecycle', 'data',
                                       'updated_at'])
            for sibling in siblings:
                sibling.delete()
            print(f'{family}: merged {len(siblings)} sibling row(s)')
    print('Migration applied.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
