"""Catalog integrity checks served to the System page.

The composed-name uniqueness invariant is enforced by monitoring, not
a DB constraint (docs/PCS_COMPOSED_NAME_INTEGRITY.md step 4, operator
decision 2026-08-11): any collision from any writer surfaces here
within one refresh cycle instead of crashing the writer.

The sample axis is physics vocabulary (operator ruling 2026-08-12,
retracting the slug-sample scheme): a machine token in ``sample_name``
is an error from any writer, surfaced here the same way.
"""

import re

from django.db.models import Count

MACHINE_SAMPLE_RE = re.compile(r'^(src-)?[0-9a-f]{12}$')


def composed_name_integrity():
    """(status, summary, data) for the composed-name-integrity collector.

    A composed name held by more than one dataset row is a broken
    identity: every task URL, API lookup, and MCP tool keys on it.
    Zero collisions is the invariant restored by the 2026-08-11
    backfill; any regrowth is an error, never normal.
    """
    from .models import Dataset

    collisions = list(
        Dataset.objects.values('composed_name')
        .annotate(n=Count('id'))
        .filter(n__gt=1)
        .order_by('-n')[:20]
    )
    total = Dataset.objects.count()
    sampled = Dataset.objects.exclude(sample_name='').count()
    machine_samples = [
        {'name': name, 'sample': sample}
        for name, sample in Dataset.objects.exclude(sample_name='')
        .values_list('composed_name', 'sample_name')
        if MACHINE_SAMPLE_RE.match(sample)
    ]
    data = {
        'colliding_names': len(collisions),
        'worst': [
            {'name': row['composed_name'], 'datasets': row['n']}
            for row in collisions[:10]
        ],
        'machine_samples': len(machine_samples),
        'machine_sample_rows': machine_samples[:10],
        'datasets': total,
        'with_sample_name': sampled,
    }
    problems = []
    if collisions:
        problems.append(f'{len(collisions)} composed names held by '
                        f'multiple datasets — identity ambiguity has '
                        f'regrown')
    if machine_samples:
        problems.append(f'{len(machine_samples)} datasets carry a '
                        f'machine token in the sample axis — the '
                        f'retracted slug-sample scheme has regrown')
    if problems:
        summary = '; '.join(problems)
        _capcom_integrity_notice(summary)
        return ('error', summary, data)
    return ('ok',
            f'composed names unique across {total} datasets '
            f'({sampled} sample-discriminated, samples all physics)',
            data)


def _capcom_integrity_notice(summary):
    """Buffer one Capcom alarm per ET day while the invariant is broken
    (in-process notice-store write; feed consumers drain it from their
    own side). A failed write is logged by the collector machinery,
    never fatal to the check itself."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from monitor_app.models import CapcomNotice

    et_today = datetime.now(ZoneInfo('America/New_York')).date().isoformat()
    dedup_key = f'composed-name-integrity:{et_today}'
    try:
        if CapcomNotice.objects.filter(dedup_key=dedup_key).exists():
            return
        CapcomNotice.objects.create(
            source='swf-catalog-integrity',
            severity='alarm',
            title='PCS composed-name integrity broken',
            detail=summary,
            url='https://epic-devcloud.org/prod/panda/system/',
            dedup_key=dedup_key,
        )
    except Exception:
        import logging
        logging.getLogger('pcs.integrity').exception(
            'capcom integrity notice write failed')
