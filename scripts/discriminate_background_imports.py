"""Discriminate background-bearing past-import rows that share a composed name.

Applies the recorded family decisions (docs/PCS_COMPOSED_NAME_FAMILIES.md)
to already-ingested rows, using the same derivation the importer now runs
at ingest (``pcs.services._past_background_discrimination``): Bkg_-prefixed
overlay variants bind an OVERLAY background (k) tag (group 2); standalone
BACKGROUNDS rows take the machine-setting token string as their sample
variant (the group-7 shape). Targets are the members of currently
duplicated composed names that carry neither a background tag nor a
sample. Rows outside the two background families are listed and left
untouched; residual duplicates after the plan are printed for manual
review. Nothing is deleted.

Dry-run by default; --apply writes. An audit JSON records every planned
and applied change.

Run under the venv with the swf-monitor project on the path:

    cd <swf-monitor>/src && source <venv>/bin/activate && source ~/.env
    python <swf-epicprod>/scripts/discriminate_background_imports.py [--apply]
"""

import argparse
import json
import os
import sys
import time
from collections import Counter

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402

django.setup()

from django.db import transaction  # noqa: E402
from django.db.models import Count  # noqa: E402

from pcs.models import Dataset  # noqa: E402
from pcs.physics_match import derive_physics  # noqa: E402
from pcs.services import (  # noqa: E402
    _past_arrival_discrimination,
    find_or_create_background_tag,
    find_or_create_evgen_tag,
)

AUDIT_DIR = '/data/wenauseic/swf-delivery'


def _arrival_context(ds):
    """(remainder, derived) the importer saw for this row, from metadata."""
    past = (ds.metadata or {}).get('past_output') or {}
    path = past.get('path') or {}
    remainder = path.get('path_remainder', '')
    beam = (past.get('filters') or {}).get('beam', '')
    derived = derive_physics(remainder, beam=beam) if remainder else None
    return remainder, derived


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--apply', action='store_true',
                        help='write changes (default: dry run)')
    args = parser.parse_args()

    dup_names = list(
        Dataset.objects.values('composed_name')
        .annotate(n=Count('id')).filter(n__gt=1)
        .values_list('composed_name', flat=True))
    members = list(
        Dataset.objects.filter(composed_name__in=dup_names)
        .select_related('background_tag')
        .order_by('composed_name', 'created_at'))

    plans, untouched = [], []
    for ds in members:
        if ds.background_tag_id is not None or ds.sample_name:
            untouched.append((ds, 'already discriminated'))
            continue
        remainder, derived = _arrival_context(ds)
        disc = _past_arrival_discrimination(remainder, derived)
        if disc:
            plans.append((ds, disc))
        else:
            untouched.append((ds, f'no discriminating axis in {remainder!r}'))

    audit = {'stamp': time.strftime('%Y%m%d-%H%M%S'), 'apply': args.apply,
             'duplicated_names': len(dup_names), 'plans': [], 'untouched': []}

    print(f'duplicated composed names: {len(dup_names)}; '
          f'member rows: {len(members)}; planned: {len(plans)}; '
          f'untouched: {len(untouched)}')
    for ds, disc in plans:
        kinds = [k for k, label in (('background_params', 'k-tag'),
                                    ('evgen_params', 'evgen'),
                                    ('sample', 'sample')) if disc.get(k)]
        kind = '+'.join('k-tag' if k == 'background_params'
                        else 'evgen' if k == 'evgen_params' else 'sample'
                        for k in kinds)
        val = (disc.get('background_params', {}).get('bg_generator', '')
               or disc.get('sample', '')
               or str(disc.get('evgen_params', '')))
        print(f'  {kind:12} dataset {ds.pk}  {ds.composed_name}  <- {val}')
        audit['plans'].append({
            'pk': ds.pk, 'composed_name': ds.composed_name,
            'dataset_name': ds.dataset_name, 'kind': kind, 'value': val,
            'disc': disc})
    for ds, why in untouched:
        audit['untouched'].append({
            'pk': ds.pk, 'composed_name': ds.composed_name, 'why': why})

    # Residual projection: names still duplicated if the plan lands. The
    # projection key mirrors the composed-name axes the plan would change.
    projected = Counter()
    plan_by_pk = {x.pk: d for x, d in plans}
    for ds in members:
        planned = plan_by_pk.get(ds.pk, {})
        key = (ds.composed_name,
               planned.get('sample', ''),
               planned.get('background_params', {}).get('bg_generator', ''),
               str(planned.get('evgen_params', '')))
        projected[key] += 1
    residual = {k: n for k, n in projected.items() if n > 1}
    if residual:
        print(f'residual duplicates after plan ({len(residual)}) — manual review:')
        for k, n in residual.items():
            print(f'  {n}x {k}')
    audit['residual_after_plan'] = len(residual)

    applied = 0
    if args.apply:
        with transaction.atomic():
            for ds, disc in plans:
                if disc.get('background_params'):
                    k_tag, _act = find_or_create_background_tag(
                        disc['background_params'],
                        created_by='background_discrimination')
                    ds.background_tag = k_tag
                if disc.get('evgen_params'):
                    e_tag, _act = find_or_create_evgen_tag(
                        disc['evgen_params'],
                        created_by='background_discrimination')
                    ds.evgen_tag = e_tag
                if disc.get('sample'):
                    ds.sample_name = disc['sample']
                ds.save()
                applied += 1
        remaining = (Dataset.objects.values('composed_name')
                     .annotate(n=Count('id')).filter(n__gt=1).count())
        print(f'applied: {applied}; duplicated composed names now: {remaining}')
        audit['applied'] = applied
        audit['duplicated_names_after'] = remaining
    else:
        print('dry run — nothing written; rerun with --apply to execute')

    os.makedirs(AUDIT_DIR, exist_ok=True)
    path = os.path.join(
        AUDIT_DIR, f"background_discrimination_audit_{audit['stamp']}.json")
    with open(path, 'w') as f:
        json.dump(audit, f, indent=2, default=str)
    print(f'audit: {path}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
