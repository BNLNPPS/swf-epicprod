#!/usr/bin/env python3
"""apply_group7_samples.py — the last slug rows get their recorded
disposition.

What remains slug-sampled after the fold and the evgen binds are the
background machine-setting families — group 7 of
PCS_COMPOSED_NAME_FAMILIES.md, recorded disposition: **sample, the
release/current/runtime token string**. This script derives that
sample from each row's source path (the segments after the stage area,
minus the token the bound evgen tag already carries) and applies it.
Rows whose derived sample makes them identical to another row are
physical duplicates and fold: outputs entries onto the survivor's
identity task, bookkeeping tasks and row deleted (the fold rule of
fold_slug_rows.py). Anything still unresolved is reported, never
guessed.

Dry-run by default; ``--apply`` executes. Django-bootstrap standalone
script — also usable by hand.
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

from pcs.models import Dataset, PandaTasks, ProdTask  # noqa: E402
from pcs.physics_config import _source_path  # noqa: E402
from pcs.reconcile import _identity_task, _upsert_task_output  # noqa: E402

SLUG_RE = re.compile(r'^(src-)?[0-9a-f]{12}$')
AREAS = ('BEAMGAS', 'SYNRAD', 'MERGED', 'BACKGROUNDS')


def derived_sample(row):
    """The group-7 sample: path segments after the area, minus the
    token the bound evgen tag already names."""
    path = _source_path(row)
    segs = [s for s in path.split('/') if s]
    start = 0
    for i, seg in enumerate(segs):
        if seg in AREAS:
            start = i + 1
    tail = segs[start:]
    while tail and tail[0] == 'EVGEN':
        tail = tail[1:]
    generator = ''
    if row.evgen_tag_id:
        generator = str((row.evgen_tag.parameters or {})
                        .get('generator', '')).lower()
    tokens = [t for t in tail
              if not (generator and generator in t.lower())]
    return '.'.join(tokens)


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
             'rses': [], 'file_count': row.file_count,
             'bytes': row.data_size,
             'complete': bool(past.get('complete')),
             'checked_at': ''}]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()

    rows = [d for d in Dataset.objects.select_related(
                'campaign', 'evgen_tag')
            if SLUG_RE.match(d.sample_name or '')]
    taken = set(Dataset.objects.exclude(
        pk__in=[d.pk for d in rows]).values_list('composed_name',
                                                 flat=True))
    plan = {'sample': [], 'fold': [], 'unresolved': []}
    named = {}
    for d in sorted(rows, key=lambda r: r.pk):
        sample = derived_sample(d)
        if not sample:
            plan['unresolved'].append({'pk': d.pk,
                                       'name': d.composed_name})
            continue
        suffix = f'.{d.sample_name}'
        base = d.composed_name[:-len(suffix)] \
            if d.composed_name.endswith(suffix) else d.composed_name
        candidate = f'{base}.{sample}'
        if candidate in named:
            survivor = named[candidate]
            entry = {'pk': d.pk, 'from': d.composed_name,
                     'into': candidate, 'survivor_pk': survivor.pk}
            plan['fold'].append(entry)
            if args.apply:
                task = _identity_task(survivor.composed_name)
                for output in outputs_from_row(d):
                    if task is not None:
                        _upsert_task_output(task, output)
                for rtask in ProdTask.objects.filter(dataset=d):
                    if PandaTasks.objects.filter(prod_task=rtask).exists():
                        rtask.dataset = survivor
                        rtask.save(update_fields=['dataset'])
                    else:
                        rtask.delete()
                d.delete()
            continue
        if candidate in taken:
            plan['unresolved'].append({'pk': d.pk, 'name': d.composed_name,
                                       'candidate': candidate,
                                       'reason': 'collides outside'})
            continue
        plan['sample'].append({'pk': d.pk, 'from': d.composed_name,
                               'to': candidate, 'sample': sample})
        if args.apply:
            d.sample_name = sample
            d.save()
        named[candidate] = d
        taken.add(candidate)

    print('TOTALS ' + json.dumps({k: len(v) for k, v in plan.items()}))
    for e in plan['sample'][:8]:
        print(' sample', e['from'][:65], '->', e['sample'])
    for e in plan['fold'][:5]:
        print(' fold', e['from'][:65], '->', e['into'][:65])
    for e in plan['unresolved']:
        print(' UNRESOLVED', e)
    print('mode: ' + ('APPLIED' if args.apply else 'dry-run'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
