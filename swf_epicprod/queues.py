"""The queue registry (docs/QUEUE_REGISTRY.md): each production queue's
status as one record, SysConfig ``production.queues``, which the front,
the regulation guards, the canaries, the NERSC balance and the room all
read through this module, so that opening, closing or retiring a queue is
one change.

Until the record is adopted it is derived, read only, from the settings
it replaces (``front.queues``, ``front.closed_queues``, the queue lists of
the room's ``comp.sites``) and the retirement and pool below, so the
readers' answers do not change with the switch itself. Adoption writes the
derived record, keeps the replaced settings under ``ARCHIVE_KEY`` and
removes them; the first write to the record adopts it.
"""
import copy
import logging
import time
from collections.abc import Mapping
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

KEY = 'production.queues'
ARCHIVE_KEY = 'production.queues.adopted_from'
REPLACED_KEYS = ('front.queues', 'front.closed_queues')
ROOM_KEY = 'comp.sites'
# production: open to new work. commissioning: not a production queue yet,
# open to work placed by hand (trials, canaries). off: closed for now.
# retired: closed for good; the work already there drains in place.
# not_built: named, not yet usable.
STATUSES = ('production', 'commissioning', 'off', 'retired', 'not_built')
# Refuse new work: the front never recommends the queue, and PCS refuses to
# place, submit, rerun, queue, trial or move work there.
CLOSED = ('off', 'retired')
# What the room shows for a status; None is the room's own reading of PanDA.
ROOM_STATUS = {'production': None, 'commissioning': 'off', 'off': 'off',
               'not_built': 'not_built'}
FIELDS = ('queue', 'status', 'reason', 'since', 'by', 'site', 'pool', 'front')
CACHE_S = 10

# The settings the record replaces, as they stood on 2026-10-23: the
# derivation's defaults when a setting is missing or malformed.
SEED_FRONT_QUEUES = ['UM_GREX_PanDA_1', 'NERSC_Perlmutter_epic', 'BNL_ePIC_GOOGLE',
                     'BNL_OSG_PanDA_1', 'NERSC_Perlmutter_epic_es', 'BNL_ePIC_GOOGLE_es']
SEED_CLOSED = {
    'BNL_ePIC_GOOGLE': 'Google is off (Torre 2026-10-19); not a production queue now',
    'BNL_ePIC_GOOGLE_es': 'Google is off (Torre 2026-10-19); not a production queue now',
    'NERSC_Perlmutter_epic': 'NERSC allocation: m3763 CPU 90% or more used',
    'NERSC_Perlmutter_epic_es': 'NERSC allocation: m3763 CPU 90% or more used',
}
SEED_ROOM = [
    {'id': 'osg', 'queues': [{'id': 'BNL_OSG_PanDA_1'}]},
    {'id': 'grex', 'queues': [{'id': 'UM_GREX_PanDA_1'}]},
    {'id': 'compute-canada', 'queues': [{'id': 'ComputeCanada_PanDA_1', 'status': 'not_built',
                                         'status_reason': 'awaiting SFU identity mapping at the CEs'}]},
    {'id': 'nersc', 'queues': [{'id': 'NERSC_Perlmutter_epic', 'status': 'off'},
                               {'id': 'NERSC_Perlmutter_epic_es', 'status': 'off'}]},
    {'id': 'google', 'queues': [{'id': 'BNL_ePIC_GOOGLE', 'status': 'off'},
                                {'id': 'BNL_ePIC_GOOGLE_es', 'status': 'off'}]},
    {'id': 'jlab', 'queues': [{'id': 'JLAB_PanDA_1', 'status': 'off'}]},
]
# regulation.RETIRED_QUEUES and CAPACITY_POOLS as they stood. PROD_1's
# existing tasks drain in place; their jobs still share OSG's capacity.
SEED_RETIRED = {
    'BNL_OSG_EPIC_PROD_1': ('closed to new tasks; existing tasks drain in place. '
                            'Use BNL_OSG_PanDA_1 for new work (Torre Wenaus, 2026-10-22)'),
}
SEED_SITES = {'BNL_OSG_EPIC_PROD_1': 'osg'}
SEED_POOLS = {'osg': ('BNL_OSG_PanDA_1', 'BNL_OSG_EPIC_PROD_1')}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _list_of_names(value):
    return isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value)


def derive(config):
    """The record the replaced settings in ``config`` describe. Pure."""
    front = config.get('front.queues')
    if not _list_of_names(front):
        if front is not None:
            logger.error('front.queues is not a list of queue names: %r; using the 2026-10-23 list', front)
        front = SEED_FRONT_QUEUES
    closed = config.get('front.closed_queues')
    if not isinstance(closed, dict):
        if closed is not None:
            logger.error('front.closed_queues is not a {queue: reason} mapping: %r; '
                         'using the 2026-10-23 closures', closed)
        closed = SEED_CLOSED
    room = config.get(ROOM_KEY)
    if not (isinstance(room, list) and any(isinstance(s, dict) and s.get('queues') for s in room)):
        room = SEED_ROOM
    room_q = {}
    for s in room:
        for q in (s.get('queues') or []) if isinstance(s, dict) else []:
            if isinstance(q, dict) and isinstance(q.get('id'), str):
                room_q.setdefault(q['id'], (s.get('id'), q.get('status'), q.get('status_reason') or ''))
    pool_of = {q: pool for pool, members in SEED_POOLS.items() for q in members}

    order = []
    for q in [*front, *closed, *SEED_RETIRED, *room_q]:
        q = str(q or '').strip()
        if q and q not in order:
            order.append(q)
    out = []
    for q in order:
        site, room_status, room_reason = room_q.get(q, (SEED_SITES.get(q), None, ''))
        if q in SEED_RETIRED:
            status, reason = 'retired', SEED_RETIRED[q]
        elif q in closed:
            status, reason = 'off', str(closed[q] or 'closed by the operator')
        elif room_status == 'not_built':
            status, reason = 'not_built', room_reason
        elif room_status == 'off':
            status, reason = 'commissioning', room_reason
        else:
            status, reason = 'production', ''
        out.append({'queue': q, 'status': status, 'reason': reason, 'since': None, 'by': None,
                    'site': site, 'pool': pool_of.get(q),
                    'front': q in front and status != 'retired'})
    return out


SEED = derive({})


def valid(value):
    """The stored record, if it is one; None otherwise."""
    if not isinstance(value, list):
        return None
    seen = set()
    for e in value:
        if not (isinstance(e, dict) and isinstance(e.get('queue'), str) and e['queue']
                and e.get('status') in STATUSES and e['queue'] not in seen):
            return None
        seen.add(e['queue'])
    return value


def _config():
    from monitor_app.models import SysConfig
    return SysConfig.get_config()


def _read():
    try:
        config = _config()
    except Exception as exc:
        from django.conf import settings
        if settings.configured:
            logger.exception('queue registry unreadable; using the 2026-10-23 record')
        else:
            logger.debug('queue registry outside Django (%s); using the 2026-10-23 record', exc)
        return copy.deepcopy(SEED)
    stored = config.get(KEY)
    if stored is None:
        return derive(config)
    if valid(stored) is None:
        logger.error('%s is malformed: %r; using the 2026-10-23 record', KEY, stored)
        return copy.deepcopy(SEED)
    return [{f: e.get(f) for f in FIELDS} | {'front': bool(e.get('front'))} for e in stored]


_cache = {'at': None, 'value': None}


def invalidate():
    _cache['at'] = None


def entries():
    """The record: one entry per queue, in the front's order."""
    t = time.monotonic()
    if _cache['at'] is None or t - _cache['at'] > CACHE_S:
        _cache['value'], _cache['at'] = _read(), t
    return copy.deepcopy(_cache['value'])


def get(queue):
    q = str(queue or '').strip()
    return next((e for e in entries() if e['queue'] == q), None)


def status(queue):
    e = get(queue)
    return e['status'] if e else None


def regulated():
    """The queues the pressure front regulates, in order."""
    return [e['queue'] for e in entries() if e['front'] and e['status'] != 'retired']


def closed():
    """{queue: reason} for every queue closed to new work."""
    return {e['queue']: e['reason'] or f"{e['status']} (docs/QUEUE_REGISTRY.md)"
            for e in entries() if e['status'] in CLOSED}


def retired():
    return {e['queue']: e['reason'] or 'retired' for e in entries() if e['status'] == 'retired'}


def pools():
    """{pool: (queue, ...)}: the queues that share one set of slots."""
    out = {}
    for e in entries():
        if e.get('pool'):
            out.setdefault(e['pool'], []).append(e['queue'])
    return {p: tuple(qs) for p, qs in out.items()}


def jedi_pools():
    """The JEDI throttler's ``POOLS`` the record implies: each pool's
    retired queues generate apart, in ``<pool>_drain``, so a drain's
    backlog never holds back the open queues' refill (PROD_1_DRAIN.md)."""
    parts = []
    for pool, members in pools().items():
        by = {e['queue']: e['status'] for e in entries()}
        live = [q for q in members if by.get(q) != 'retired']
        drain = [q for q in members if by.get(q) == 'retired']
        parts += [f"{pool}:{','.join(live)}"] if live else []
        parts += [f"{pool}_drain:{','.join(drain)}"] if drain else []
    return ';'.join(parts)


def room_queues():
    """{site: [entry, ...]} for the room: every queue but the retired."""
    out = {}
    for e in entries():
        if e['status'] != 'retired' and e.get('site'):
            out.setdefault(e['site'], []).append(e)
    return out


class LiveMapping(Mapping):
    """A read-only mapping recomputed from the record on each use, for the
    names readers imported as constants (``regulation.RETIRED_QUEUES``)."""

    def __init__(self, build):
        self._build = build

    def __getitem__(self, key):
        return self._build()[key]

    def __iter__(self):
        return iter(self._build())

    def __len__(self):
        return len(self._build())

    def __contains__(self, key):
        return key in self._build()

    def __repr__(self):
        return f'LiveMapping({self._build()!r})'


# Writing

def adopt_into(data, by, *, strip_room=False):
    """Make ``data`` (the SysConfig document, held under its row lock)
    carry the record. Returns (entries, adopted): ``adopted`` when this
    call wrote it, keeping the replaced settings under ``ARCHIVE_KEY``.
    ``strip_room`` also drops the queue lists from ``comp.sites``, once
    the room reads its queues from the record."""
    stored = valid(data.get(KEY))
    adopted = stored is None
    if adopted:
        if data.get(KEY) is not None:
            raise ValueError(f'{KEY} is malformed; repair it on the System page first')
        stored = derive(data)
        archive = {'at': _now(), 'by': by}
        archive.update({k: data[k] for k in REPLACED_KEYS if k in data})
        data[ARCHIVE_KEY] = archive
        for k in REPLACED_KEYS:
            data.pop(k, None)
        data[KEY] = stored
    if strip_room and isinstance(data.get(ROOM_KEY), list) and any(
            isinstance(s, dict) and 'queues' in s for s in data[ROOM_KEY]):
        data.setdefault(ARCHIVE_KEY, {'at': _now(), 'by': by})[ROOM_KEY] = data[ROOM_KEY]
        data[ROOM_KEY] = [{k: v for k, v in s.items() if k != 'queues'} for s in data[ROOM_KEY]]
    return stored, adopted


def change(record, queue, new_status, *, reason='', by='', **fields):
    """Set one queue's status in ``record`` (a list of entries), adding the
    queue if it is new. Returns its previous status (None if new)."""
    if new_status not in STATUSES:
        raise ValueError(f'status {new_status!r} is not one of {", ".join(STATUSES)}')
    unknown = set(fields) - set(FIELDS)
    if unknown:
        raise ValueError(f'unknown fields {sorted(unknown)}')
    e = next((x for x in record if x['queue'] == queue), None)
    before = e['status'] if e else None
    if e is None:
        e = {'queue': queue, 'site': None, 'pool': None, 'front': False}
        record.append(e)
    e.update(status=new_status, reason='' if new_status == 'production' else str(reason or ''),
             since=_now(), by=str(by or ''))
    e.update(fields)
    if new_status == 'retired':
        e['front'] = False
    return before


def write(mutate, by):
    """Run ``mutate(record)`` on the stored record under the SysConfig row
    lock and save when it returns something true, adopting the record
    first if need be. Returns mutate's result."""
    from django.db import transaction
    from monitor_app.models import SysConfig
    with transaction.atomic():
        obj, _ = SysConfig.objects.select_for_update().get_or_create(
            id=1, defaults={'config_data': {}})
        record, adopted = adopt_into(obj.config_data, by)
        result = mutate(record)
        if not result:
            return result
        obj.config_data[KEY] = record
        obj.updated_by = by
        obj.save()
    invalidate()
    if adopted:
        _log_adoption(by)
    return result


def adopt(by, *, strip_room=False):
    """Adopt the record now (scripts/queue-status.py --adopt)."""
    from django.db import transaction
    from monitor_app.models import SysConfig
    with transaction.atomic():
        obj, _ = SysConfig.objects.select_for_update().get_or_create(
            id=1, defaults={'config_data': {}})
        before = copy.deepcopy(obj.config_data.get(ROOM_KEY))
        record, adopted = adopt_into(obj.config_data, by, strip_room=strip_room)
        stripped = obj.config_data.get(ROOM_KEY) != before
        if adopted or stripped:
            obj.updated_by = by
            obj.save()
    invalidate()
    if adopted:
        _log_adoption(by)
    return record, adopted, stripped


def set_status(queue, new_status, *, reason='', by, **fields):
    """Set a queue's status; recorded as a ``queue_status`` action."""
    before = write(lambda record: change(record, queue, new_status, reason=reason, by=by, **fields)
                   or 'new', by)
    from monitor_app.epicprod_logging import log_epicprod_action
    log_epicprod_action(
        'ops-agent', 'queue_status', username=by, outcome='ok', sublevel='normal',
        subject_type='queue', subject_key=queue,
        message=f"{queue}: {before} -> {new_status}{': ' + reason if reason else ''} (by {by})")
    return before


def _log_adoption(by):
    from monitor_app.epicprod_logging import log_epicprod_action
    log_epicprod_action(
        'ops-agent', 'sysconfig_edit', username=by, outcome='ok', sublevel='normal',
        summary=f'adopted {KEY}',
        message=(f'sysconfig_edit ok — adopted {KEY} from {", ".join(REPLACED_KEYS)} and the '
                 f'retirement constants; the replaced settings are kept under {ARCHIVE_KEY} '
                 '(docs/QUEUE_REGISTRY.md)'))
