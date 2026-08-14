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

from pcs.models import Dataset, ProdTask  # noqa: E402
from pcs.physics_match import derive_physics  # noqa: E402
from pcs.services import (  # noqa: E402
    _ensure_csvimport_anchors,
    _ensure_r0_stage_tag,
    _ensure_s0_stage_tag,
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

    # Phase 1 — stage ruling (operator, 2026-08-14): FULL simulation
    # outputs carry r0 (not reconstructed); the r axis separates a
    # production's FULL and RECO datasets. Applied class-wide to every
    # archive FULL row still on the reconstruction anchor.
    _, _, simu_anchor, reco_anchor, _, _ = _ensure_csvimport_anchors()
    r0 = _ensure_r0_stage_tag(created_by='stage_ruling')
    s0 = _ensure_s0_stage_tag(created_by='stage_ruling')
    stage_rows = list(Dataset.objects.filter(
        dataset_name__startswith='past.FULL.', reco_tag=reco_anchor))
    print(f'phase 1 (stage r0): {len(stage_rows)} FULL rows on the reco anchor')
    evgen_rows = list(Dataset.objects.filter(metadata__stage='evgen')
                      .filter(simu_tag=simu_anchor) |
                      Dataset.objects.filter(metadata__stage='evgen')
                      .filter(reco_tag=reco_anchor))
    print(f'phase 1b (stage s0.r0): {len(evgen_rows)} EVGEN rows on anchors')
    if args.apply:
        with transaction.atomic():
            for ds in stage_rows:
                ds.reco_tag = r0
                ds.save()
            for ds in evgen_rows:
                if ds.simu_tag_id == simu_anchor.pk:
                    ds.simu_tag = s0
                if ds.reco_tag_id == reco_anchor.pk:
                    ds.reco_tag = r0
                ds.save()

    # Phase 2 — decision-table discrimination for members of duplicated
    # composed names.
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

    # Phase 3 — republication fold. Groups still sharing a composed name
    # after stage and discrimination are the same output listed at a
    # reorganized archive path (added directory levels, retry directories):
    # the oldest row is the dataset; newer nightly re-mints fold into it.
    # The keeper takes the newest listing's path and counts, retains prior
    # paths in metadata, and the re-mint rows and their past_output tasks
    # are deleted. A group with no nightly_cron re-mint is listed for
    # manual review, never touched.
    folds, manual = [], []
    post_dups = (Dataset.objects.values('composed_name')
                 .annotate(n=Count('id')).filter(n__gt=1)
                 .values_list('composed_name', flat=True))
    for name in post_dups:
        rows = sorted(Dataset.objects.filter(composed_name=name),
                      key=lambda r: (r.created_at, r.pk))
        keeper, extras = rows[0], rows[1:]
        # HARD INTERLOCK: a FULL row and a RECO row are different data
        # products and are NEVER fold partners; a stage-mixed group means
        # stage discrimination has not landed and folding would delete
        # real data (the 2026-08-14 incident). Manual review only.
        stages = {r.dataset_name.split('.')[1] for r in rows
                  if r.dataset_name.startswith('past.')}
        if len(stages) > 1:
            manual.append(name)
        elif all(r.created_by == 'nightly_cron' for r in extras):
            folds.append((keeper, extras))
        else:
            manual.append(name)
    print(f'phase 3 (republication fold): {len(folds)} groups fold; '
          f'{len(manual)} need manual review')
    for keeper, extras in folds:
        for r in extras:
            print(f'  fold dataset {r.pk} ({r.dataset_name}) '
                  f'-> {keeper.pk} ({keeper.dataset_name})  [{keeper.composed_name}]')
    for name in manual:
        print(f'  manual: {name}')
    audit['folds'] = [
        {'keeper': k.pk, 'keeper_name': k.dataset_name,
         'composed_name': k.composed_name,
         'folded': [{'pk': r.pk, 'dataset_name': r.dataset_name,
                     'src': ((r.metadata or {}).get('source') or {}).get('location', '')}
                    for r in extras]}
        for k, extras in folds]
    audit['manual_review'] = manual

    if args.apply:
        folded = 0
        with transaction.atomic():
            for keeper, extras in folds:
                meta = dict(keeper.metadata or {})
                past = dict(meta.get('past_output') or {})
                alts = list(past.get('alternate_paths') or [])
                keeper_src = (meta.get('source') or {}).get('location', '')
                newest = max(extras, key=lambda r: (r.created_at, r.pk))
                n_meta = newest.metadata or {}
                n_src = (n_meta.get('source') or {}).get('location', '')
                if keeper_src and n_src and keeper_src != n_src:
                    alts.append(keeper_src)
                    meta['source'] = dict(n_meta.get('source') or {})
                    past.update(dict(n_meta.get('past_output') or {}))
                past['alternate_paths'] = alts
                meta['past_output'] = past
                keeper.metadata = meta
                keeper.file_count = newest.file_count
                keeper.data_size = newest.data_size
                keeper.save()
                for r in extras:
                    ProdTask.objects.filter(
                        name=r.dataset_name, status='past_output').delete()
                    r.delete()
                    folded += 1
        remaining = (Dataset.objects.values('composed_name')
                     .annotate(n=Count('id')).filter(n__gt=1).count())
        print(f'folded: {folded}; duplicated composed names now: {remaining}')
        audit['folded'] = folded
        audit['duplicated_names_final'] = remaining
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
