"""Remove datasets from one disk RSE, only where tape holds a complete copy
(docs/STORAGE.md, Removing datasets from an RSE).

A removal expires the dataset's rule on the RSE (lifetime 0), the same
act as an operator's ``update_replication_rule``: the rule goes at once
and the deletion daemon removes the replicas no other rule on that RSE
holds. It is immediate and cannot be undone.

A dataset is eligible only when the tape RSE holds all of it: a rule
whose expression is the tape RSE, in state OK, no lock replicating or
stuck, and as many locks OK as the dataset has files. A suspended or
stuck tape rule means the tape copy was never made, and removing the
disk copy would lose the data. The tape check is Rucio's lock state at
the tape RSE, not a read of the tape itself. A dataset with no rule on
the RSE has nothing to remove; its replicas there are held by some other
expression and are reported, not touched.

Production datasets stay open, so a dataset whose producing task is
still active is kept: its tape rule may be OK for the files attached so
far while new files arrive, and those would lose their disk lock before
reaching tape. Task state comes from the caller (``task_states``, the
storage pass's reading of PCS and PanDA); a dataset whose task state was
not read is kept, and one with no recorded production task is eligible
on its tape copy alone.

``plan`` reads and changes nothing; ``execute`` takes the names a plan
found eligible, checks each again against the live catalog, and expires
only those that still pass. Both are used by the operator CLI
(scripts/rse-delete.py) and by the production operations agent, which
runs them for the monitor's storage page as ``eicprod`` with a token
from the PanDA server (jlab_auth.rucio_client).
"""

import fnmatch
import os

SCOPE = os.environ.get('EPICPROD_RSE_DELETION_SCOPE', 'epic')
TAPE_RSE = os.environ.get('EPICPROD_TAPE_RSE', 'JLAB-TAPE-SE')
# The disk RSEs a removal may name: production storage only.
DISK_RSES = tuple(r.strip() for r in os.environ.get(
    'EPICPROD_RSE_DELETION_RSES', 'BNL-XRD,EIC-XRD').split(',') if r.strip())
# A pattern names at least this many literal path segments before its
# first wildcard, so '/RECO/*' cannot sweep a whole tier.
MIN_LITERAL_SEGMENTS = 2
MAX_DATASETS = 5000


class RseDeletionError(Exception):
    """A request refused before any catalog change."""


def validate(rse, pattern):
    """The RSE and pattern a removal may be asked for, or RseDeletionError."""
    rse = str(rse or '').strip()
    pattern = str(pattern or '').strip()
    if rse == TAPE_RSE:
        raise RseDeletionError(f'{rse} is the tape RSE; removals keep tape.')
    if rse not in DISK_RSES:
        raise RseDeletionError(
            f'{rse or "(none)"} is not a production disk RSE; '
            'removals may name ' + ', '.join(DISK_RSES) + '.')
    if not pattern.startswith('/'):
        raise RseDeletionError('The pattern is a dataset name starting with /.')
    literal = []
    for segment in pattern.strip('/').split('/'):
        if any(c in segment for c in '*?['):
            break
        literal.append(segment)
    if len([s for s in literal if s]) < MIN_LITERAL_SEGMENTS:
        raise RseDeletionError(
            f'The pattern must name at least {MIN_LITERAL_SEGMENTS} path '
            'segments before its first wildcard, e.g. /RECO/26.03.0/*.')
    return rse, pattern


def tape_copy(rules, length):
    """(complete, summary) for a dataset's tape rules."""
    tape = [r for r in rules if r.get('rse_expression') == TAPE_RSE]
    if not tape:
        return False, f'no rule on {TAPE_RSE}'
    for r in tape:
        if (r.get('state') == 'OK' and not r.get('locks_replicating_cnt')
                and not r.get('locks_stuck_cnt')
                and length and (r.get('locks_ok_cnt') or 0) >= length):
            return True, f'{TAPE_RSE} rule OK, {r.get("locks_ok_cnt")}/{length} files'
    r = tape[0]
    return False, (f'{TAPE_RSE} rule {r.get("state")}: '
                   f'{r.get("locks_ok_cnt") or 0}/{length} files OK, '
                   f'{r.get("locks_replicating_cnt") or 0} replicating, '
                   f'{r.get("locks_stuck_cnt") or 0} stuck')


def _read_task_states(task_states, names):
    """{name: 'active'|'final'|None|'unread'}; 'unread' for every name
    when there is no reader. A reader that raises fails the request."""
    if task_states is None:
        return {n: 'unread' for n in names}
    states = task_states(names)
    return {n: states.get(n) for n in names}


def assess(client, rse, name, task_state='unread'):
    """One dataset's removal record from the live catalog."""
    # Counted from the content: an open dataset carries no stored length.
    did = client.get_did(SCOPE, name, dynamic_depth='FILE')
    length = int(did.get('length') or 0)
    rules = list(client.list_did_rules(SCOPE, name))
    on_rse = [r for r in rules if r.get('rse_expression') == rse]
    complete, tape = tape_copy(rules, length)
    item = {
        'name': name,
        'files': length,
        'bytes': int(did.get('bytes') or 0),
        'rules': [r['id'] for r in on_rse],
        'other_rules': sorted({r.get('rse_expression') for r in rules
                               if r.get('rse_expression') not in (rse, TAPE_RSE)}),
        'tape': tape,
        'tape_complete': complete,
        'open': bool(did.get('open')),
        'task_state': task_state,
    }
    if not on_rse:
        item.update(eligible=False, reason=f'no rule on {rse}; nothing to remove')
    elif not complete:
        item.update(eligible=False, reason=f'tape copy incomplete ({tape})')
    elif task_state == 'active':
        item.update(eligible=False, reason='its production task is still active')
    elif task_state == 'unread':
        item.update(eligible=False, reason='production task state not read')
    else:
        item.update(eligible=True, reason='')
    return item


def _totals(items):
    eligible = [i for i in items if i['eligible']]
    return {
        'datasets': len(items),
        'eligible': len(eligible),
        'refused': len(items) - len(eligible),
        'eligible_files': sum(i['files'] for i in eligible),
        'eligible_bytes': sum(i['bytes'] for i in eligible),
    }


def plan(client, rse, pattern, task_states=None):
    """What a removal of ``pattern`` from ``rse`` would do. Changes nothing."""
    rse, pattern = validate(rse, pattern)
    names = sorted(client.list_dids(SCOPE, {'name': pattern}, did_type='dataset'))
    if len(names) > MAX_DATASETS:
        raise RseDeletionError(
            f'{len(names)} datasets match; at most {MAX_DATASETS} per request. '
            'Narrow the pattern.')
    states = _read_task_states(task_states, names)
    items = []
    for name in names:
        try:
            items.append(assess(client, rse, name, states[name]))
        except Exception as e:                                # noqa: BLE001
            items.append({'name': name, 'files': 0, 'bytes': 0, 'rules': [],
                          'other_rules': [], 'tape': '', 'tape_complete': False,
                          'open': None, 'task_state': states[name],
                          'eligible': False, 'reason': f'catalog read failed: {e}'})
    return {'rse': rse, 'pattern': pattern, 'scope': SCOPE, 'tape_rse': TAPE_RSE,
            'datasets': items, 'totals': _totals(items)}


def totals(outcomes, requested):
    """The counts of a removal's outcomes so far."""
    removed = [o for o in outcomes if o['outcome'] == 'removed']
    return {
        'requested': requested,
        'removed': len(removed),
        'skipped': sum(o['outcome'] == 'skipped' for o in outcomes),
        'failed': sum(o['outcome'] == 'failed' for o in outcomes),
        'removed_files': sum(o.get('files', 0) for o in removed),
        'removed_bytes': sum(o.get('bytes', 0) for o in removed),
    }


def execute(client, rse, pattern, names, task_states=None, on_outcome=None):
    """Expire ``rse``'s rule on each of ``names`` that still passes. Each
    name is checked again live, task state included, and must match
    ``pattern``; one that no longer passes is skipped with its reason.
    ``on_outcome`` is called with each dataset's outcome as it is decided,
    so a removal cut short leaves the record of what it removed. Returns
    per-dataset outcomes and totals; never raises for one dataset's
    failure."""
    rse, pattern = validate(rse, pattern)
    states = _read_task_states(task_states, list(names))
    outcomes = []

    def emit(outcome):
        outcomes.append(outcome)
        if on_outcome:
            on_outcome(outcome)

    for name in names:
        if not fnmatch.fnmatchcase(name, pattern):
            emit({'name': name, 'outcome': 'skipped',
                  'reason': 'does not match the pattern'})
            continue
        try:
            item = assess(client, rse, name, states[name])
        except Exception as e:                                # noqa: BLE001
            emit({'name': name, 'outcome': 'failed',
                  'reason': f'catalog read failed: {e}'})
            continue
        if not item['eligible']:
            emit({'name': name, 'outcome': 'skipped',
                  'reason': item['reason'], 'tape': item['tape']})
            continue
        expired, errors = [], []
        for rule_id in item['rules']:
            try:
                client.update_replication_rule(rule_id=rule_id,
                                               options={'lifetime': 0})
                expired.append(rule_id)
            except Exception as e:                            # noqa: BLE001
                errors.append(f'{rule_id}: {e}')
        # A partial expiry (some of several rules) counts as failed and
        # names the rules it did expire.
        emit({
            'name': name, 'files': item['files'], 'bytes': item['bytes'],
            'rules': expired, 'tape': item['tape'],
            'outcome': 'failed' if errors else 'removed',
            'reason': '; '.join(errors),
        })
    return {'rse': rse, 'pattern': pattern, 'datasets': outcomes,
            'totals': totals(outcomes, len(names))}
