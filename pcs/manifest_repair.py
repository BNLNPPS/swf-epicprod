"""Explicit repair of an attempt record from its exact sandbox manifest.

Preparation reads only. Applying requires the reviewed row count, physics
event sum and canonical digest, with a lock and action log supplied by the
maintenance command. Residual previews never call this repair.
"""
from copy import deepcopy
from datetime import datetime, timezone

from . import manifests


def prepare(attempt):
    """Read the exact CSV and retain the original record for a guarded write."""
    metadata = attempt.metadata or {}
    previous = {k: deepcopy(metadata.get(k))
                for k in ('manifest', 'manifest_rejected')}
    rows = manifests.fetch_from_cache(attempt.jedi_task_id)
    count_error = None
    try:
        submitted = manifests.attempt_row_count(attempt.jedi_task_id)
    except manifests.ManifestUnavailable as exc:
        submitted, count_error = None, str(exc)
    if submitted is not None and len(rows) != submitted:
        raise manifests.ManifestUnavailable(
            f'sandbox has {len(rows)} rows, but the attempt declares {submitted}')
    record = manifests.make_record(rows, 'cache')
    summary = {'task_name': attempt.task_name,
               'jedi_task_id': attempt.jedi_task_id,
               'rows': len(rows), 'events': sum(n for _f, _e, n, _c in rows),
               'sha256': record['sha256'], 'submitted_rows': submitted,
               'count_basis': 'submitted rows and exact CSV' if submitted is not None
                              else 'exact sandbox CSV alone',
               'count_error': count_error,
               'previous_rows': (previous['manifest'] or {}).get('rows')}
    return {'record': record, 'previous': previous, 'summary': summary}


def replacement(metadata, prepared, *, rows, events, sha256, actor, reason):
    """Preserve concurrent metadata and retain the superseded record as audit."""
    summary = prepared['summary']
    if (type(rows) is not int or type(events) is not int
            or (rows, events, sha256) !=
            (summary['rows'], summary['events'], summary['sha256'])):
        raise ValueError('row count, event sum or canonical digest differs from the reviewed CSV')
    if not actor or not reason.strip():
        raise ValueError('repair requires an actor and a reason')
    current = metadata or {}
    if any(current.get(k) != v for k, v in prepared['previous'].items()):
        raise ValueError('the manifest record changed during verification; dry-run again')
    result = deepcopy(current)
    history = result.get('manifest_superseded') or []
    if not isinstance(history, list):
        history = [history]
    history.append({**deepcopy(prepared['previous']), 'by': actor,
                    'at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                    'reason': reason.strip(),
                    'replacement': {k: summary[k] for k in
                                    ('rows', 'events', 'sha256', 'count_basis', 'submitted_rows')}})
    result['manifest_superseded'] = history
    result['manifest'] = deepcopy(prepared['record'])
    result['manifest']['sandbox_verification'] = {
        'rows': summary['rows'], 'sha256': summary['sha256'],
        'by': actor, 'at': history[-1]['at']}
    result.pop('manifest_rejected', None)
    return result
