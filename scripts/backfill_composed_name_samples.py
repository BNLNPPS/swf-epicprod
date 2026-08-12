"""Composed-name backfill: execute the family decision table.

docs/PCS_COMPOSED_NAME_FAMILIES.md decisions (operator, 2026-08-11)
applied to every colliding sample-less dataset:

- DIS minQ2 and ALP mass families: rebind the physics tag through the
  extended derivation (minQ2 tokens, decimal-safe path).
- Bkg_-prefixed overlays: rebind physics the same way and bind a k
  background tag (background_type OVERLAY, the Bkg token as
  bg_generator, beams from the derivation).
- DVCS polarization variants: sample from the process-variant and
  polarization tokens.
- Single-particle scans: sample from the angle token (the existing
  sample precedent).
- Beam-gas machine settings and any residual collision: sample from
  the underived remainder tokens.
- DVMP generator versions: rebind the EVGEN tag per generator version.
- past.* assimilated rows: mechanical sample from the source slug.
- Unparsable names: listed, untouched.

Dry-run by default; --apply writes. The dry run prints every planned
change including proposed new tags, and the residual-collision count
the plan would leave.

Run under the venv with the swf-monitor project on the path:

    cd <swf-monitor>/src && source <venv>/bin/activate && source ~/.env
    python <swf-epicprod>/scripts/backfill_composed_name_samples.py [--apply]
"""

import argparse
import os
import re
import sys
from collections import Counter, defaultdict

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402

django.setup()

from django.db import transaction  # noqa: E402

from pcs.models import Dataset  # noqa: E402
from pcs.name_tokens import sample_name_reserved_collision  # noqa: E402
from pcs.physics_match import derive_physics, taskname_remainder_path  # noqa: E402
from pcs.services import (  # noqa: E402
    _intake_sample_candidate,
    find_or_create_background_tag,
    find_or_create_evgen_tag,
    find_or_create_physics_tag,
)

TASKNAME_RE = re.compile(r'group\.EIC\.[\d.]+\.\w+\.(.+)$')
PAST_RE = re.compile(r'^past\.\w+\.\d+\.\d+\.\d+\.(.+)$')
CSV_IMPORT_RE = re.compile(r'^csv_import\.(.+)$')
BEAM_RE = re.compile(r'\.(\d+x\d+)(\.|$)')
POL_RE = re.compile(r'^e[mp]h[LT][mp]$')
DVCS_VARIANTS = ('BH_ONLY', 'DVCS_BH', 'DVCS_ONLY')
EPIC_VERSION_RE = re.compile(r'^EpIC(?:_v)?([\d.\-]+)$')
ANGLE_RE = re.compile(r'^\d+to\d+deg$')


def _derived_for(remainder):
    beam_match = BEAM_RE.search(remainder)
    beam = beam_match.group(1) if beam_match else ''
    return derive_physics(taskname_remainder_path(remainder), beam=beam) or {}


def _plan_for(d, remainder, derived, apply_mode):
    """One dataset's planned change: dict with any of physics_tag,
    background_params, evgen_params, sample."""
    plan = {}
    tokens = taskname_remainder_path(remainder).split('/')

    if remainder.startswith('Bkg_'):
        plan['background_params'] = {
            'background_type': 'OVERLAY',
            'bg_generator': remainder.split('.DIS')[0].split('.SIDIS')[0],
            'bg_source': '', 'bg_mechanism': '',
            'beam_energy_electron': derived.get('beam_energy_electron', ''),
            'beam_energy_hadron': derived.get('beam_energy_hadron', ''),
        }

    if 'DVCS' in str(derived.get('process', '')):
        picked = [t for t in tokens if t in DVCS_VARIANTS or POL_RE.match(t)]
        if picked:
            plan['sample'] = '.'.join(picked)
            return plan

    if derived.get('process') == 'SINGLE':
        angles = [t for t in tokens if ANGLE_RE.match(t)]
        if angles:
            plan['sample'] = angles[0]
            return plan

    epic_versions = [m.group(1) for t in tokens
                     if (m := EPIC_VERSION_RE.match(t))]
    if epic_versions and 'DVMP' in str(derived.get('process', '')):
        plan['evgen_params'] = {
            'generator': 'EpIC', 'generator_version': epic_versions[0]}
        return plan

    if derived and derived.get('process') not in ('BEAMGAS', 'SYNRAD'):
        tag, action = find_or_create_physics_tag(
            derived, created_by='composed_name_backfill',
            dry_run=not apply_mode)
        plan['physics_tag'] = (tag, action, derived)
    elif derived:
        candidate = _intake_sample_candidate(remainder, derived)
        if candidate:
            plan['sample'] = candidate
    return plan


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    args = ap.parse_args()

    names = Counter(
        d.composed_name for d in Dataset.objects.filter(sample_name=''))
    colliding = {n for n, c in names.items() if c > 1}
    targets = [d for d in (Dataset.objects.filter(sample_name='')
                           .select_related('physics_tag', 'evgen_tag'))
               if d.composed_name in colliding]

    stats = Counter()
    unparsable = []
    plans = []
    for d in targets:
        dn = d.dataset_name or ''
        slug_match = PAST_RE.match(dn) or CSV_IMPORT_RE.match(dn)
        if slug_match:
            slug = slug_match.group(1)
            if sample_name_reserved_collision(slug):
                # A slug shaped like a reserved token (e.g. b<digits>, the
                # block vocabulary) keeps its uniqueness under a prefix.
                slug = f'src-{slug}'
                stats['slug_prefixed'] += 1
            plans.append((d, {'sample': slug}))
            stats['slug_sample'] += 1
            continue
        m = TASKNAME_RE.match(dn)
        if not m:
            unparsable.append((d.pk, dn))
            continue
        remainder = m.group(1)
        derived = _derived_for(remainder)
        plan = _plan_for(d, remainder, derived, args.apply)
        if not plan:
            stats['no_plan'] += 1
            unparsable.append((d.pk, dn))
            continue
        plans.append((d, plan))
        for key in plan:
            stats[key] += 1

    print(f'targets: {len(targets)} | planned: {len(plans)} | '
          f'unparsable/no-plan: {len(unparsable)}')
    for key, count in sorted(stats.items()):
        print(f'  {key}: {count}')

    new_physics = {}
    for d, plan in plans:
        entry = plan.get('physics_tag')
        if entry and entry[1] == 'create':
            key = tuple(sorted(entry[2].items()))
            new_physics.setdefault(key, entry[2])
    if new_physics:
        print(f'new physics tags to create: {len(new_physics)}')
        for params in new_physics.values():
            print(f'  {params}')
    bg_params = {tuple(sorted(p['background_params'].items()))
                 for _d, p in plans if 'background_params' in p}
    if bg_params:
        print(f'k background tags to bind: {len(bg_params)}')
        for key in sorted(bg_params):
            print(f'  {dict(key)}')

    # Residual-collision simulation: composed base + planned changes.
    simulated = Counter()
    planned_by_pk = {d.pk: plan for d, plan in plans}
    for d in targets:
        plan = planned_by_pk.get(d.pk, {})
        entry = plan.get('physics_tag')
        if entry and entry[0] is not None:
            p_label = entry[0].tag_label
        elif entry:
            p_label = f'pNEW{hash(tuple(sorted(entry[2].items()))) & 0xffff}'
        else:
            p_label = d.physics_tag.tag_label
        e_label = (f'eNEW{plan["evgen_params"]["generator_version"]}'
                   if 'evgen_params' in plan else d.evgen_tag.tag_label)
        k_label = (f'kNEW{hash(tuple(sorted(plan["background_params"].items()))) & 0xffff}'
                   if 'background_params' in plan else '')
        sample = plan.get('sample', '')
        sim = (f'{d.detector_version}.{d.detector_config}.{p_label}.{e_label}'
               + (f'.{k_label}' if k_label else '')
               + (f'.{sample}' if sample else ''))
        simulated[sim] += 1
    residual = {n: c for n, c in simulated.items() if c > 1}
    print(f'residual colliding identities after plan: {len(residual)}')
    for n, c in residual.items():
        print(f'  {c}x {n}')

    if unparsable:
        print('unparsable / no plan:')
        for pk, dn in unparsable:
            print(f'  dataset {pk}: {dn}')

    if not args.apply:
        print('dry run — nothing written; rerun with --apply to execute')
        return 0

    applied = 0
    with transaction.atomic():
        for d, plan in plans:
            entry = plan.get('physics_tag')
            if entry:
                tag, _action, _derived = entry
                d.physics_tag = tag
            if 'background_params' in plan:
                k_tag, _act = find_or_create_background_tag(
                    plan['background_params'],
                    created_by='composed_name_backfill')
                d.background_tag = k_tag
            if 'evgen_params' in plan:
                e_tag, _act = find_or_create_evgen_tag(
                    plan['evgen_params'],
                    created_by='composed_name_backfill')
                d.evgen_tag = e_tag
            if plan.get('sample'):
                d.sample_name = plan['sample']
            d.save()
            applied += 1
    print(f'applied: {applied}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
