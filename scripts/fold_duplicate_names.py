#!/usr/bin/env python3
"""fold_duplicate_names.py — fold same-composed-name duplicate rows.

Repair for rows that share a composed name (the 2026-08-13 nightly
minted several sets through the reconciler's unguarded create path,
since closed): for each duplicated name, the oldest row is the
identity; every later row's payload moves to the identity task as
outputs entries (constructed from the row's recorded metadata where it
has no task), its bookkeeping tasks delete (tasks with PanDA
associations re-point), and the row deletes. Physics configurations
left with no editions delete afterward.

Dry-run by default with a printed plan and audit JSON;
``--apply`` executes.
"""
import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(THIS_DIR, '..', '..', 'swf-monitor', 'src'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402
django.setup()

from pcs.models import Dataset, PandaTasks, PhysicsConfig, ProdTask  # noqa: E402
from pcs.reconcile import _identity_task, _upsert_task_output  # noqa: E402

AUDIT_DIR = '/data/wenauseic/swf-delivery'


def outputs_from_row(row):
    entries = []
    for task in ProdTask.objects.filter(dataset=row):
        entries.extend((task.overrides or {}).get('outputs') or [])
    if entries:
        return entries
    meta = row.metadata or {}
    location = (meta.get('source') or {}).get('location', '')
    if not location:
        return []
    past = meta.get('past_output') or {}
    return [{'did': location, 'stage': past.get('stage', ''),
             'version': past.get('version', ''),
             'filters': past.get('filters', {}),
             'rses': [{'rse': r.get('name', ''), 'files': r.get('files', 0),
                       'total': r.get('total', 0),
                       'complete': r.get('status') == 'complete'}
                      for r in past.get('rses') or []],
             'file_count': row.file_count, 'bytes': row.data_size,
             'complete': bool(past.get('complete')), 'checked_at': ''}]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--created-since', default='',
                        help='fold only duplicate rows created on/after '
                             'this ISO date; older duplicates are '
                             'reported and left for review')
    args = parser.parse_args()

    names = Counter(Dataset.objects.values_list('composed_name', flat=True))
    dup_names = [n for n, c in names.items() if c > 1]
    plan = []
    for name in sorted(dup_names):
        rows = list(Dataset.objects.filter(composed_name=name)
                    .order_by('created_at', 'pk'))
        keeper, extras = rows[0], rows[1:]
        entry = {'name': name, 'keeper_pk': keeper.pk,
                 'keeper_created': str(keeper.created_at),
                 'folded': [], 'held': []}
        if args.created_since:
            held = [r for r in extras
                    if str(r.created_at) < args.created_since]
            extras = [r for r in extras
                      if str(r.created_at) >= args.created_since]
            entry['held'] = [{'pk': r.pk, 'dataset_name': r.dataset_name,
                              'created_at': str(r.created_at)}
                             for r in held]
        for row in extras:
            entry['folded'].append({
                'pk': row.pk, 'dataset_name': row.dataset_name,
                'created_by': row.created_by,
                'created_at': str(row.created_at),
                'outputs_moved': len(outputs_from_row(row)),
                'metadata': row.metadata})
            if args.apply:
                task = _identity_task(keeper.composed_name)
                for output in outputs_from_row(row):
                    if task is not None:
                        _upsert_task_output(task, output)
                for rtask in ProdTask.objects.filter(dataset=row):
                    if PandaTasks.objects.filter(prod_task=rtask).exists():
                        rtask.dataset = keeper
                        rtask.save(update_fields=['dataset'])
                    else:
                        rtask.delete()
                row.delete()
        plan.append(entry)

    orphans = 0
    if args.apply:
        qs = PhysicsConfig.objects.filter(editions__isnull=True)
        orphans = qs.count()
        qs.delete()

    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    audit_path = os.path.join(AUDIT_DIR, f'dup_fold_audit_{stamp}.json')
    with open(audit_path, 'w') as fh:
        json.dump(plan, fh, indent=1, default=str)
    print('TOTALS ' + json.dumps({
        'duplicated_names': len(dup_names),
        'rows_folded': sum(len(e['folded']) for e in plan),
        'orphan_pcs_deleted': orphans}))
    for e in plan[:6]:
        print(' ', e['name'][:70], '| keeper', e['keeper_pk'],
              '| folds', [f['pk'] for f in e['folded']])
    print(f'audit: {audit_path}')
    print('mode: ' + ('APPLIED' if args.apply else 'dry-run'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
