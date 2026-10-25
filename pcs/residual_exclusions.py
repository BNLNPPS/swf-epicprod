"""Explicit exclusions for one residual rerun, bound to its reviewed rows.

The preview signs its candidate set and ordered manifest identity. Enqueue
stores an independent snapshot with the queued operation; submission copies
it to the attempt. No persistent task-wide exclusion is maintained.
"""
import hashlib
from collections import defaultdict

from django.core import signing
from django.utils import timezone

from . import manifests

SALT = 'pcs.residual-exclusions.v1'
MAX_AGE = 3600


def manifest_identity(rows):
    # Sequence numbers depend on order, unlike the manifest's set digest.
    return hashlib.sha256('\n'.join(manifests.format_rows(rows)).encode()).hexdigest()


def preview(task, attempt, rows, info, undelivered):
    """Sign a read-only preview; failed simulation exits are candidates."""
    from .services import panda_attempt_bad_input_seqs
    if not info.get('exact'):
        return {'unavailable': 'Row exclusions require an exact attempt manifest.'}
    seqs, evidence = panda_attempt_bad_input_seqs(attempt.jedi_task_id)
    formatted = manifests.format_rows(rows)
    pending = set(undelivered)
    candidates = sorted(s for s in (seqs or ())
                        if 1 <= s <= len(rows) and formatted[s - 1] in pending)
    context = {'task': task.pk, 'of': attempt.jedi_task_id,
               'manifest_sha256': manifest_identity(rows),
               'manifest_rows': len(rows), 'candidates': candidates}
    result = {'rows': len(candidates), 'evidence': evidence,
              'evidence_kind': 'suspected', 'jedi_task_id': attempt.jedi_task_id,
              'token': signing.dumps(context, salt=SALT, compress=True)}
    if seqs is None:
        result['unavailable'] = evidence
    return result


def _attempt_rows(task, context):
    """Validate against retained exact records without fetching or storing."""
    from .services import ServiceError
    if context.get('task') != task.pk:
        raise ServiceError('This residual preview belongs to another task.', status=409)
    try:
        attempt = manifests.choose_attempt(task, context.get('of'))
    except manifests.ManifestUnavailable as exc:
        raise ServiceError(str(exc), status=409) from exc
    record = manifests.record_of(attempt)
    if not record or record.get('source') == 'reconstructed':
        raise ServiceError('The exact preview manifest is no longer available; preview again.', status=409)
    rows = manifests.expand(record)
    if (len(rows) != context.get('manifest_rows')
            or manifest_identity(rows) != context.get('manifest_sha256')):
        raise ServiceError('The manifest changed since preview; preview again.', status=409)
    return attempt, rows


def snapshot(task, token, *, bad_input=False, listed=None, changed_by=''):
    """Freeze exactly the chosen preview rows, with no task or log write."""
    from .services import ServiceError
    if not isinstance(token, str) or not token:
        raise ServiceError('A current residual preview is required.', status=409)
    try:
        context = signing.loads(token, salt=SALT, max_age=MAX_AGE)
    except (signing.BadSignature, TypeError, ValueError) as exc:
        raise ServiceError('Residual preview is invalid or expired; preview again.', status=409) from exc
    attempt, rows = _attempt_rows(task, context)
    selected = set(context['candidates']) if bad_input else set()
    if listed:
        by_key = defaultdict(list)
        for seq, (file, _ext, _events, chunk) in enumerate(rows, 1):
            by_key[file, int(chunk)].append(seq)
        if not isinstance(listed, list):
            raise ServiceError('exclude_rows must be a list of file/chunk records.', status=400)
        for item in listed:
            try:
                file, chunk_text = str(item['file']).strip(), str(item['chunk']).strip()
                if not chunk_text.isdigit():
                    raise ValueError('Chunk must be a non-negative integer.')
                chunk = int(chunk_text)
            except (KeyError, TypeError, ValueError) as exc:
                raise ServiceError('Each excluded row needs a file and integer chunk.', status=400) from exc
            matches = by_key.get((file, chunk), [])
            if len(matches) != 1:
                raise ServiceError(f'Excluded file/chunk has {len(matches)} matches in the preview manifest: {file}, {chunk}.', status=409)
            selected.update(matches)
    if not selected:
        raise ServiceError('No rows were selected for exclusion.', status=409)
    return {'of': attempt.jedi_task_id, 'manifest_sha256': context['manifest_sha256'],
            'manifest_rows': len(rows), 'seq_numbers': sorted(selected),
            'source': ('suspected_simulation_exit1+explicit_list' if bad_input and listed
                       else 'suspected_simulation_exit1' if bad_input else 'explicit_list'),
            'evidence_kind': 'suspected' if bad_input else 'operator_selected',
            'by': changed_by or '', 'at': timezone.now().isoformat()}


def validate_snapshot(task, selection):
    """Fail closed if an accepted snapshot's source manifest changed."""
    context = dict(selection, task=task.pk)
    _attempt_rows(task, context)


def excluded_rows(attempt, rows, selection):
    """Resolve an immutable snapshot against its exact ordered work units."""
    if not selection:
        return set()
    if (selection.get('of') != attempt.jedi_task_id
            or selection.get('manifest_rows') != len(rows)
            or selection.get('manifest_sha256') != manifest_identity(rows)):
        raise ValueError('Residual exclusion does not match this attempt manifest; preview again.')
    seqs = selection.get('seq_numbers') or []
    if any(type(s) is not int or not 1 <= s <= len(rows) for s in seqs):
        raise ValueError('Residual exclusion contains invalid sequence numbers.')
    return set(seqs)
