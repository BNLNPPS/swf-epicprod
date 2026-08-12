"""Composed-name collision audit: the family table for backfill review.

docs/PCS_COMPOSED_NAME_INTEGRITY.md step 3: every colliding composed
name traces to sample-less datasets. This audit groups the colliding
taskname-born datasets into review families (digit-normalized remainder
pattern), and for each family reports the members, the varying
discriminator values, the current physics-tag binding, whether
derive_physics captures the discriminator, and a dry-run tag match
(reuse of an existing tag vs creation) both as derived and with the
discriminator treated as the q2 axis. The past.* assimilated class
(hash-named, no physics remainder) and unparsable names are counted
and listed separately.

Read-only; emits Markdown to stdout.

Run under the venv with the swf-monitor project on the path:

    cd <swf-monitor>/src && source <venv>/bin/activate && source ~/.env
    python <swf-epicprod>/scripts/audit_composed_name_families.py
"""

import os
import re
import sys
from collections import Counter, defaultdict

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402

django.setup()

from pcs.models import Dataset  # noqa: E402
from pcs.physics_match import derive_physics  # noqa: E402
from pcs.services import find_or_create_physics_tag  # noqa: E402

TASKNAME_RE = re.compile(r'group\.EIC\.[\d.]+\.\w+\.(.+)$')
BEAM_RE = re.compile(r'\.(\d+x\d+)(\.|$)')
Q2ISH_RE = re.compile(r'(minq2|q2)', re.IGNORECASE)


def _pattern(remainder):
    return re.sub(r'\d+', 'N', remainder)


def _varying_tokens(remainders):
    """The token positions whose values differ across the family."""
    split = [r.split('.') for r in remainders]
    width = min(len(s) for s in split)
    varying = []
    for i in range(width):
        values = sorted({s[i] for s in split})
        if len(values) > 1:
            varying.append((i, values))
    return varying


def _derived_for(remainder):
    beam_match = BEAM_RE.search(remainder)
    beam = beam_match.group(1) if beam_match else ''
    return derive_physics(remainder.replace('.', '/'), beam=beam) or {}


def _dry_match(derived):
    if not derived:
        return 'no-derivation'
    if derived.get('process') in ('BEAMGAS', 'SYNRAD'):
        return 'background (signal-free tag + k-tag domain)'
    try:
        tag, action = find_or_create_physics_tag(derived, dry_run=True)
    except Exception as exc:
        return f'match error: {exc}'
    if action == 'reuse':
        return f'reuse p{tag.tag_number}'
    return 'create'


def main():
    names = Counter(
        d.composed_name for d in Dataset.objects.filter(sample_name=''))
    colliding = {n for n, c in names.items() if c > 1}

    families = defaultdict(list)
    past_rows = 0
    unparsed = []
    pattern_of = {}
    name_members = defaultdict(list)
    for d in (Dataset.objects.filter(sample_name='')
              .select_related('physics_tag', 'campaign')):
        if d.composed_name not in colliding:
            continue
        dn = d.dataset_name or ''
        if dn.startswith('past.'):
            past_rows += 1
            name_members[d.composed_name].append('past.*')
            continue
        m = TASKNAME_RE.match(dn)
        if not m:
            unparsed.append((d.pk, dn))
            name_members[d.composed_name].append('(unparsable)')
            continue
        pattern = _pattern(m.group(1))
        families[pattern].append((d, m.group(1)))
        pattern_of[d.pk] = pattern
        name_members[d.composed_name].append(pattern)

    print('# Composed-name collision families — backfill review table')
    print()
    print(f'Colliding sample-less datasets: taskname-born '
          f'{sum(len(v) for v in families.values())} in {len(families)} '
          f'families; past.* assimilated {past_rows}; '
          f'unparsable {len(unparsed)}.')
    print()

    for pattern, members in sorted(
            families.items(), key=lambda kv: -len(kv[1])):
        remainders = sorted({rem for _d, rem in members})
        datasets = [d for d, _rem in members]
        campaigns = sorted({d.detector_version for d in datasets})
        tags = sorted({f'p{d.physics_tag.tag_number}' for d in datasets
                       if d.physics_tag_id})
        varying = _varying_tokens(remainders)
        derived = _derived_for(remainders[0])
        captured = []
        for _i, values in varying:
            if all(re.fullmatch(r'\d+x\d+', v) for v in values):
                captured.append(True)  # beam axis, derived per dataset
            else:
                hit = any(v in str(dv)
                          for v in values for dv in derived.values())
                captured.append(hit)
        as_derived = _dry_match(derived)
        with_q2 = ''
        q2ish = [values for (_i, values) in varying
                 if all(Q2ISH_RE.search(v) for v in values)]
        if q2ish:
            trial = dict(derived)
            trial['q2_range'] = q2ish[0][0]
            with_q2 = _dry_match(trial)

        print(f'## {pattern}')
        print(f'- datasets: {len(datasets)} | campaigns: '
              f'{", ".join(campaigns)} | current tags: '
              f'{", ".join(tags) or "none"}')
        for (i, values), hit in zip(varying, captured):
            shown = ', '.join(values[:12]) + (' …' if len(values) > 12 else '')
            print(f'- varying token {i}: {shown} '
                  f'({"captured by derivation" if hit else "NOT captured"})')
        if not varying:
            print('- varying token: none within the family pattern '
                  '(collision spans beams or campaigns)')
        print(f'- dry-run tag match as derived: {as_derived}'
              + (f' | with discriminator as q2 axis: {with_q2}'
                 if with_q2 else ''))
        partner_patterns = Counter()
        for d in datasets:
            for p in name_members[d.composed_name]:
                if p != pattern:
                    partner_patterns[p] += 1
        if partner_patterns:
            shown = ' | '.join(f'{p} ({c})' for p, c
                               in partner_patterns.most_common(5))
            print(f'- collides with other patterns: {shown}')
        else:
            print('- collisions are intra-family only')
        print(f'- example: {remainders[0]}')
        print()

    if unparsed:
        print('## Unparsable names')
        for pk, dn in unparsed:
            print(f'- dataset {pk}: {dn}')
        print()
    print('## past.* assimilated class')
    print(f'- {past_rows} hash-named datasets; no physics remainder to '
          'judge. Proposed rule: mechanical sample from each row\'s '
          'unique source slug.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
