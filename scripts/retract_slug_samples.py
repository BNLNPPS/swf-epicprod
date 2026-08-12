#!/usr/bin/env python3
"""retract_slug_samples.py — classify and retract mechanical slug samples.

The 2026-08-12 retraction of the slug-sample scheme: the sample axis is
physics vocabulary and never carries bookkeeping tokens. This script
classifies every dataset whose ``sample_name`` is a mechanical slug
(the 12-hex token of the slug rule, with or without the ``src-``
prefix) and reports the retraction action per row:

- **fold**: the row is a reconciler-created physical sibling of an
  existing edition (same configuration key, another edition row
  present). Its payload already lives in the edition task's outputs
  entries; the row is redundant identity and folds away.
- **empty**: the row is the sole edition of its configuration and its
  composed name is unique without any sample — the sample empties.
- **disposition**: emptying the sample would recreate a composed-name
  collision; the row needs its recorded per-family disposition
  (docs/PCS_COMPOSED_NAME_FAMILIES.md) applied instead. Reported with
  its collision partners for the curation pass.

Dry-run by default: a JSON report and a printed summary, no writes.
``--apply-empty`` applies only the empty class. The fold and
disposition classes are applied by their own reviewed steps, never
here. Django-bootstrap standalone script — also usable by hand.

Usage::

    cd /data/wenauseic/github/swf-monitor/src
    source ../../swf-testbed/.venv/bin/activate && source ~/.env
    python ../../swf-epicprod/scripts/retract_slug_samples.py \
        [--campaign NAME] [--report PATH] [--apply-empty]
"""
import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(THIS_DIR, '..', '..', 'swf-monitor', 'src'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402
django.setup()

from pcs.models import Dataset  # noqa: E402
from pcs.physics_config import physics_config_key  # noqa: E402

SLUG_RE = re.compile(r'^(src-)?[0-9a-f]{12}$')


def stripped_name(row):
    """The composed name with the slug sample segment removed."""
    suffix = f'.{row.sample_name}'
    if row.composed_name.endswith(suffix):
        return row.composed_name[:-len(suffix)]
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--campaign', default='',
                        help='restrict to one campaign name')
    parser.add_argument('--report',
                        default='/tmp/slug_sample_retraction.json',
                        help='JSON report path')
    parser.add_argument('--apply-empty', action='store_true',
                        help='apply the empty class (sample -> blank); '
                             'fold and disposition classes are never '
                             'applied here')
    args = parser.parse_args()

    qs = Dataset.objects.select_related(
        'campaign', 'physics_tag', 'evgen_tag', 'simu_tag', 'reco_tag',
        'background_tag')
    if args.campaign:
        qs = qs.filter(campaign__name=args.campaign)

    all_rows = list(qs)
    slug_rows = [d for d in all_rows if SLUG_RE.match(d.sample_name or '')]

    # Configuration-key twins: an edition of the same configuration
    # whose row is NOT slug-sampled marks a slug row as a foldable
    # physical sibling.
    key_members = defaultdict(list)
    for d in all_rows:
        try:
            key_members[physics_config_key(d)['key']].append(d)
        except Exception as exc:  # noqa: BLE001
            print(f'WARNING: config key failed for {d.pk} '
                  f'({d.composed_name}): {exc}', file=sys.stderr)

    # Name uniqueness once slug samples come off: candidate names of
    # slug rows compete with every untouched composed name and with
    # each other.
    taken = Counter(d.composed_name for d in all_rows
                    if d not in slug_rows)
    candidates = Counter()
    for d in slug_rows:
        name = stripped_name(d)
        if name:
            candidates[name] += 1

    report = {'fold': [], 'empty': [], 'disposition': [], 'odd': []}
    for d in slug_rows:
        entry = {
            'pk': d.pk, 'campaign': d.campaign.name if d.campaign_id
            else '', 'dataset_name': d.dataset_name,
            'composed_name': d.composed_name,
            'sample_name': d.sample_name, 'created_by': d.created_by,
        }
        name = stripped_name(d)
        if name is None:
            entry['reason'] = ('composed name does not end with the '
                               'slug sample')
            report['odd'].append(entry)
            continue
        try:
            key = physics_config_key(d)['key']
        except Exception:  # noqa: BLE001
            key = None
        twins = [m for m in key_members.get(key, [])
                 if m.pk != d.pk and not SLUG_RE.match(m.sample_name or '')]
        if twins and d.created_by == 'rucio_reconcile':
            entry['fold_into'] = twins[0].composed_name
            entry['fold_into_pk'] = twins[0].pk
            report['fold'].append(entry)
            continue
        if taken[name] == 0 and candidates[name] == 1:
            entry['new_composed_name'] = name
            report['empty'].append(entry)
            continue
        entry['collision_with'] = (
            [t for t in (taken and [n for n, c in taken.items()
                                    if n == name]) or []][:3])
        entry['collision_candidates'] = candidates[name]
        entry['stripped_name'] = name
        report['disposition'].append(entry)

    summary = {cls: len(rows) for cls, rows in report.items()}
    summary['slug_rows_total'] = len(slug_rows)
    summary['rows_scanned'] = len(all_rows)
    by_campaign = Counter()
    for cls in ('fold', 'empty', 'disposition', 'odd'):
        for entry in report[cls]:
            by_campaign[f"{entry['campaign']}:{cls}"] += 1
    summary['by_campaign'] = dict(sorted(by_campaign.items()))

    with open(args.report, 'w') as fh:
        json.dump({'summary': summary, **report}, fh, indent=1,
                  default=str)
    print('SUMMARY ' + json.dumps(summary, indent=1))
    print(f'report: {args.report}')

    if args.apply_empty:
        applied = 0
        for entry in report['empty']:
            d = Dataset.objects.get(pk=entry['pk'])
            d.sample_name = ''
            d.save()
            applied += 1
        print(f'APPLIED empty class: {applied} rows')
    return 0


if __name__ == '__main__':
    sys.exit(main())
