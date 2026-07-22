"""Move placeholder ProdConfig uses onto the dedicated placeholder row.

CSV import and direct PanDA intake historically anchored tasks to the real
'26.02.0 Standard Production' ProdConfig, which renders as a genuine
software configuration on rows that merely lack one (misstating executed
software — see EPICPROD_ASSESSMENTS.md findings). The anchor now resolves
to the dedicated placeholder row (services.PLACEHOLDER_PRODCONFIG_NAME);
this one-off migration moves existing placeholder uses there.

A placeholder use is a task pointing at the 26.02.0 row whose campaign is
not 26.02.0 AND whose creator is mechanical (auto-intake, past ingest,
imports). Tasks of the 26.02.0 campaign keep their genuine config, and
human-created rows are reported but never migrated — a person may have
bound that config deliberately, or the row is compose-state awaiting a
real binding.

Run under the venv with the swf-monitor project on the path:

    cd <swf-monitor>/src && source <venv>/bin/activate && source ~/.env
    python <swf-epicprod>/scripts/migrate_placeholder_prodconfig.py [--apply]

Dry-run by default; --apply writes.
"""

import argparse
import os
import sys

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402

django.setup()

from pcs.models import ProdConfig, ProdTask  # noqa: E402
from pcs.services import PLACEHOLDER_PRODCONFIG_NAME, _ensure_csvimport_anchors  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true',
                        help='write the change (default: dry-run report)')
    args = parser.parse_args()

    legacy = ProdConfig.objects.filter(
        name__icontains='26.02.0 Standard').first()
    if legacy is None:
        print('No 26.02.0 Standard Production row found — nothing to do.')
        return 0
    if legacy.name == PLACEHOLDER_PRODCONFIG_NAME:
        print('Legacy anchor already is the placeholder — nothing to do.')
        return 0

    mechanical = ('association_sweep', 'nightly_cron', 'prodops_agent',
                  'csv_import', 'questionnaire_import')
    candidates = (ProdTask.objects
                  .filter(prod_config=legacy, created_by__in=mechanical)
                  .exclude(campaign__name='26.02.0')
                  .select_related('campaign'))
    human = (ProdTask.objects
             .filter(prod_config=legacy)
             .exclude(campaign__name='26.02.0')
             .exclude(created_by__in=mechanical)
             .select_related('campaign'))
    by_key = {}
    for task in candidates:
        key = (task.campaign.name if task.campaign else '(none)',
               task.created_by or '(unset)')
        by_key[key] = by_key.get(key, 0) + 1
    total = sum(by_key.values())
    kept = ProdTask.objects.filter(prod_config=legacy,
                                   campaign__name='26.02.0').count()
    print(f'Mechanical placeholder uses of {legacy.name!r}: {total} '
          f'(genuine 26.02.0 uses kept: {kept})')
    for (campaign, creator), count in sorted(by_key.items()):
        print(f'  {campaign:12} {creator:24} {count}')
    human_by_key = {}
    for task in human:
        key = (task.campaign.name if task.campaign else '(none)',
               task.created_by or '(unset)')
        human_by_key[key] = human_by_key.get(key, 0) + 1
    if human_by_key:
        print('Human-created rows left untouched (review manually):')
        for (campaign, creator), count in sorted(human_by_key.items()):
            print(f'  {campaign:12} {creator:24} {count}')

    if not args.apply:
        print('Dry run — rerun with --apply to migrate.')
        return 0

    # Creates the placeholder row if this is the first use.
    _, _, _, _, placeholder, _ = _ensure_csvimport_anchors()
    updated = (ProdTask.objects
               .filter(prod_config=legacy, created_by__in=mechanical)
               .exclude(campaign__name='26.02.0')
               .update(prod_config=placeholder))
    print(f'Migrated {updated} tasks to {placeholder.name!r}.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
