#!/usr/bin/env python3
"""sync_task_names_after_retraction.py — retire hash task names.

The slug-sample retraction renamed datasets (composed names recompose
on save) but ``ProdTask.name`` is stored text: tasks renamed to
slug-sampled composed names by the 2026-08-11 backfill still carry the
retracted hashes. This script renames every task whose name ends in a
mechanical slug token to its dataset's current composed name. Physical
PanDA names on associations are untouched by design; the resolver's
base-reduction keeps any externally held old name presenting the task.

Dry-run by default; ``--apply`` executes.
"""
import argparse
import json
import os
import re
import sys

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(THIS_DIR, '..', '..', 'swf-monitor', 'src'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402
django.setup()

from pcs.models import ProdTask  # noqa: E402

HASH_TAIL_RE = re.compile(r'\.(src-)?[0-9a-f]{12}$')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()

    plan = {'rename': [], 'skip': []}
    for task in ProdTask.objects.select_related('dataset').all():
        # Only composed-form names are retraction leakage; past.* and
        # csv_import.* task names are physical archival names by
        # convention and are out of scope here.
        if not (task.name or '').startswith('group.EIC'):
            continue
        if not HASH_TAIL_RE.search(task.name or ''):
            continue
        if not task.dataset_id:
            plan['skip'].append({'pk': task.pk, 'name': task.name,
                                 'reason': 'no dataset'})
            continue
        target = task.dataset.composed_name
        if task.name == target:
            continue
        if HASH_TAIL_RE.search(target):
            plan['skip'].append({'pk': task.pk, 'name': task.name,
                                 'reason': 'dataset name still hashed'})
            continue
        collision = ProdTask.objects.filter(name=target).exclude(
            pk=task.pk).exists()
        if collision:
            plan['skip'].append({'pk': task.pk, 'name': task.name,
                                 'target': target,
                                 'reason': 'target task name exists'})
            continue
        plan['rename'].append({'pk': task.pk, 'from': task.name,
                               'to': target})
        if args.apply:
            task.name = target
            task.save(update_fields=['name', 'updated_at'])

    print('TOTALS ' + json.dumps({k: len(v) for k, v in plan.items()}))
    for e in plan['rename'][:6]:
        print(' rename', e['from'][:70], '->', e['to'][:70])
    for e in plan['skip'][:10]:
        print(' SKIP', e.get('name', '')[:70], e['reason'])
    print('mode: ' + ('APPLIED' if args.apply else 'dry-run'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
