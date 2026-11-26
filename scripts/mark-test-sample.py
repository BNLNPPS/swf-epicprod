#!/usr/bin/env python3
"""mark-test-sample.py — record production-sample editions as tests.

A test belongs in a trial (docs/PCS.md, Trials): its outputs under
epic:/TEST/trial/<name>/ with a lifetime, no row of its own in the campaign
plan. A test submitted as a production sample (pcs.production_samples) has
neither. This marks such an edition as a test after the fact:

1. ``metadata['production_sample']['test']`` records who, when, why and the
   campaign the edition was in;
2. the edition's rows and its tasks leave the campaign, so the campaign plan,
   the completion record and the status grids no longer count it, as a trial
   with no campaign is not counted.

Marking deletes nothing. A lifetime on its registered outputs
(scripts/did-lifetime.py) is a deletion and needs Torre Wenaus's explicit
approval of that deletion. Only editions carrying a production-sample record
are accepted. Rows are written with queryset updates, so no save() logic
renames or rebinds them; the identity history records the change under the
actor and reason. One ``production_sample_marked_test`` action records the
run. Dry run by default; ``--apply`` writes.

    cd /data/wenauseic/github/swf-monitor/src
    ../../swf-testbed/.venv/bin/python ../../swf-epicprod/scripts/mark-test-sample.py \\
        --by NAME --reason TEXT <composed name> ... [--apply]
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone as dt_timezone

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.environ.get('SWF_MONITOR_SRC',
                                  os.path.join(THIS_DIR, '..', '..', 'swf-monitor', 'src')))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402
django.setup()

from django.db import transaction  # noqa: E402

from monitor_app.epicprod_logging import log_epicprod_action  # noqa: E402
from pcs.models import Dataset, ProdTask  # noqa: E402
from pcs.permanence import identity_change  # noqa: E402


def plan(names):
    """Per name: its rows, tasks and campaign, or the reason it is refused."""
    entries = []
    for name in names:
        rows = list(Dataset.objects.filter(composed_name=name).select_related('campaign')
                    .order_by('block_num', 'pk'))
        entry = {'name': name, 'rows': [r.pk for r in rows]}
        if not rows:
            entry['refused'] = 'no edition of that name'
        elif not all((r.metadata or {}).get('production_sample') for r in rows):
            entry['refused'] = 'not a production sample'
        else:
            entry['campaign'] = sorted({r.campaign.name for r in rows if r.campaign_id})
            entry['tasks'] = list(ProdTask.objects.filter(dataset__in=rows)
                                  .order_by('pk').values_list('name', flat=True))
            entry['marked'] = bool(((rows[0].metadata or {}).get('production_sample') or {})
                                   .get('test'))
        entries.append(entry)
    return entries


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('names', nargs='+', help='composed name of each edition')
    ap.add_argument('--by', required=True, help='who marks them')
    ap.add_argument('--reason', required=True, help='why, recorded on each edition')
    ap.add_argument('--apply', action='store_true', help='write; without this nothing is written')
    args = ap.parse_args()
    entries = plan(args.names)
    refused = [e for e in entries if e.get('refused')]
    for e in entries:
        print(json.dumps(e), file=sys.stderr)
    if refused or not args.apply:
        print(json.dumps({'applied': False, 'refused': len(refused), 'entries': entries}))
        return 1 if refused else 0
    now = datetime.now(dt_timezone.utc).isoformat()
    with transaction.atomic(), identity_change(args.by, args.reason):
        for e in entries:
            rows = Dataset.objects.select_for_update().filter(pk__in=e['rows']).order_by('pk')
            for row in rows:
                metadata = dict(row.metadata or {})
                sample = dict(metadata.get('production_sample') or {})
                sample['test'] = {'by': args.by, 'at': now, 'reason': args.reason,
                                  'campaign': ', '.join(e['campaign'])}
                metadata['production_sample'] = sample
                Dataset.objects.filter(pk=row.pk).update(metadata=metadata, campaign=None)
            ProdTask.objects.filter(dataset_id__in=e['rows']).update(campaign=None)
    log_epicprod_action(
        'script', 'production_sample_marked_test', username=args.by,
        sublevel='normal', live_default=False,
        message=f"{len(entries)} production sample(s) marked as tests: {args.reason}",
        names=args.names, reason=args.reason)
    print(json.dumps({'applied': True, 'refused': 0, 'entries': entries}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
