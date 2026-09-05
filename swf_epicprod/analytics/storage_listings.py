"""The storage record's retrieval surface (docs/STORAGE.md, Retrieval).

The storage component carries only the head of each exception listing;
the full lists are served from the pass's store by the ``epicprod_storage``
MCP tool and its REST counterpart, both thin faces over ``listing`` here.
The store is read as it stands, read-only, so a pass may be writing it
(WAL mode) while a listing is served; the envelope names the last
completed pass and any pass in progress so a reader knows how far the
record reaches. The definitions are the pass module's: a present file has
no ``gone_at``; a ghost is a present file with no replica in state
AVAILABLE, held by its replica rows in any other state, or by the
pseudo-RSE ``none`` when it has no replica row at all; a stuck rule is a
dataset rule carrying ``stuck_at``; a stalled dataset is open, its task
active, and its newest file older than the stalled threshold.
"""
import datetime as dt
import json
import logging
import os
import sqlite3
import time

from .storage import (DEFAULT_DB, NO_RSE, THRESHOLD_DEFAULTS, _parse_iso,
                      _seconds_since, thresholds)

logger = logging.getLogger(__name__)

LISTINGS = ('ghosts', 'stuck_rules', 'stalled_datasets')
DEFAULT_LIMIT = 100
MAX_LIMIT = 1000
BUSY_TIMEOUT_S = 5


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _open_readonly(path):
    db = sqlite3.connect(f'file:{path}?mode=ro', uri=True,
                         timeout=BUSY_TIMEOUT_S, check_same_thread=False)
    db.execute(f'PRAGMA busy_timeout = {BUSY_TIMEOUT_S * 1000}')
    return db


def _as_of(db):
    """The last completed pass and any pass in progress."""
    completed = db.execute(
        'SELECT id, mode, started, finished, files_checked, datasets_checked'
        ' FROM passes WHERE finished IS NOT NULL ORDER BY id DESC LIMIT 1'
    ).fetchone()
    running = db.execute(
        'SELECT id, mode, started FROM passes WHERE finished IS NULL'
        ' ORDER BY id DESC LIMIT 1').fetchone()
    block = {
        'completed_pass': ({'id': completed[0], 'mode': completed[1],
                            'started': completed[2], 'finished': completed[3],
                            'files_checked': completed[4],
                            'datasets_checked': completed[5]}
                           if completed else None),
        'running_pass': ({'id': running[0], 'mode': running[1],
                          'started': running[2]} if running else None),
    }
    if running and (completed is None or running[0] > completed[0]):
        block['note'] = ('a pass is writing the store; rows reflect its '
                         'crawl so far on top of the last completed pass')
    return block


def _clamp(limit, offset):
    try:
        limit = int(limit or DEFAULT_LIMIT)
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    try:
        offset = int(offset or 0)
    except (TypeError, ValueError):
        offset = 0
    return max(1, min(limit, MAX_LIMIT)), max(0, offset)


# ---------------------------------------------------------------------------
# Ghosts
# ---------------------------------------------------------------------------

# The ghost population is small (tens of thousands of names against
# millions of files) but finding it from the files side is a full scan
# per query, seconds now and tens of seconds at the census's size. It is
# found instead from the replicas side, where the non-available rows are
# few, plus the one scan for files holding no replica row at all, and
# held per process until the store file changes and the hold is older
# than the TTL: a running pass changes the store continuously, so the
# TTL bounds the rebuild to once per interval.
GHOST_CACHE_TTL_S = 300
_GHOST_CACHE = {'version': None, 'built': None, 'built_mono': 0.0,
                'rows': None}


def _store_version(path):
    """The newest modification time of the store and its WAL file."""
    stamps = []
    for candidate in (path, path + '-wal'):
        try:
            stamps.append(os.stat(candidate).st_mtime)
        except OSError:
            pass
    return max(stamps) if stamps else None


def _file_rows(db, names):
    """{name: file columns} for present files among ``names``."""
    out = {}
    for start in range(0, len(names), 900):
        chunk = names[start:start + 900]
        marks = ', '.join(['?'] * len(chunk))
        for name, campaign, root, location, size, created in db.execute(
                'SELECT name, campaign, root, location, bytes, created_at'
                f' FROM files WHERE gone_at IS NULL AND name IN ({marks})',
                chunk):
            out[name] = (campaign, root, location, size, created)
    return out


def _ghost_population(db, path):
    """Every ghost with its holders, oldest first, from the store or the
    per-process hold."""
    version = _store_version(path)
    cache = _GHOST_CACHE
    if (cache['rows'] is not None
            and (version == cache['version']
                 or time.monotonic() - cache['built_mono'] < GHOST_CACHE_TTL_S)):
        return cache['rows'], cache['built']
    holders = {}
    for name, rse, state in db.execute(
            "SELECT h.name, h.rse, h.state FROM replicas h"
            " WHERE h.state <> 'AVAILABLE' AND NOT EXISTS"
            " (SELECT 1 FROM replicas a WHERE a.name = h.name"
            "  AND a.state = 'AVAILABLE')"):
        holders.setdefault(name, {})[rse] = state
    files = _file_rows(db, list(holders))
    for name, campaign, root, location, size, created in db.execute(
            'SELECT f.name, f.campaign, f.root, f.location, f.bytes,'
            ' f.created_at FROM files f WHERE f.gone_at IS NULL AND NOT EXISTS'
            ' (SELECT 1 FROM replicas h WHERE h.name = f.name)'):
        files[name] = (campaign, root, location, size, created)
        holders[name] = {NO_RSE: NO_RSE}
    rows = []
    for name, (campaign, root, location, size, created) in files.items():
        rows.append({'name': name, 'campaign': campaign, 'root': root,
                     'dataset': '/' + location if location else None,
                     'bytes': int(size or 0), 'created_at': created,
                     'holders': holders[name]})
    rows.sort(key=lambda r: (r['created_at'] or '', r['name']))
    built = _now().isoformat(timespec='seconds')
    cache.update(version=version, built=built, built_mono=time.monotonic(),
                 rows=rows)
    return rows, built


def _ghosts(db, now, rse, campaign, state, limit, offset, path=DEFAULT_DB):
    population, built = _ghost_population(db, path)

    def _keep(r):
        if campaign and r['campaign'] != campaign:
            return False
        if rse == NO_RSE:
            return NO_RSE in r['holders']
        if rse:
            return rse in r['holders'] and (
                not state or r['holders'][rse] == state)
        if state:
            return state in r['holders'].values()
        return True

    selected = [r for r in population if _keep(r)]
    by_rse = {}
    for r in selected:
        for holder, hstate in r['holders'].items():
            if rse and holder != rse:
                continue
            slot = by_rse.setdefault(holder, {'files': 0, 'bytes': 0,
                                              'by_state': {},
                                              'by_campaign': {},
                                              'oldest': None})
            slot['files'] += 1
            slot['bytes'] += r['bytes']
            slot['by_state'][hstate] = slot['by_state'].get(hstate, 0) + 1
            slot['by_campaign'][str(r['campaign'])] = (
                slot['by_campaign'].get(str(r['campaign']), 0) + 1)
            if r['created_at'] and (slot['oldest'] is None
                                    or r['created_at'] < slot['oldest']):
                slot['oldest'] = r['created_at']
    for slot in by_rse.values():
        age = _seconds_since(slot['oldest'], now) if slot['oldest'] else None
        slot['oldest_age_s'] = round(age) if age is not None else None
    rows = []
    for r in selected[offset:offset + limit]:
        age = _seconds_since(r['created_at'], now) if r['created_at'] else None
        rows.append(dict(r, age_s=round(age) if age is not None else None))
    return len(selected), rows, {'by_rse': by_rse,
                                 'population_built_at': built}


# ---------------------------------------------------------------------------
# Stuck rules
# ---------------------------------------------------------------------------

def _stuck_rules(db, now, rse, campaign, state, limit, offset, path=None):
    known = [r[0] for r in db.execute('SELECT rse FROM rses')]
    where, params = ['gone_at IS NULL'], []
    if campaign:
        where.append('campaign = ?')
        params.append(campaign)
    found = []
    for name, campaign_, rules in db.execute(
            'SELECT name, campaign, rules FROM datasets WHERE '
            + ' AND '.join(where), params):
        for rule in json.loads(rules or '[]'):
            if not rule.get('stuck_at'):
                continue
            if state and str(rule.get('state') or '') != state:
                continue
            expression = str(rule.get('rse_expression') or '')
            holders = [k for k in known if k in expression] or [expression]
            for holder in holders:
                if rse and holder != rse:
                    continue
                age = _seconds_since(rule['stuck_at'], now)
                found.append({
                    'dataset': name, 'campaign': campaign_, 'rse': holder,
                    'rule_id': rule.get('id'), 'state': rule.get('state'),
                    'rse_expression': expression, 'copies': rule.get('copies'),
                    'stuck_at': rule['stuck_at'],
                    'stuck_age_s': round(age) if age is not None else None,
                    'locks_ok': rule.get('locks_ok') or 0,
                    'locks_replicating': rule.get('locks_replicating') or 0,
                    'locks_stuck': rule.get('locks_stuck') or 0,
                    'created_at': rule.get('created_at'),
                    'expires_at': rule.get('expires_at'),
                })
    found.sort(key=lambda r: (r['stuck_at'], r['dataset'], r['rse']))
    return len(found), found[offset:offset + limit], {}


# ---------------------------------------------------------------------------
# Stalled datasets
# ---------------------------------------------------------------------------

def _stalled_datasets(db, now, rse, campaign, state, limit, offset, path=None):
    th = thresholds()
    stalled_hours = float(th.get('storage_stalled_hours',
                                 THRESHOLD_DEFAULTS['storage_stalled_hours']))
    stalled_seconds = stalled_hours * 3600
    where, params = ["gone_at IS NULL AND is_open = 1"
                     " AND task_state = 'active'"], []
    if campaign:
        where.append('campaign = ?')
        params.append(campaign)
    candidates = list(db.execute(
        'SELECT name, campaign, task_state, length, bytes FROM datasets'
        ' WHERE ' + ' AND '.join(where), params))
    found = []
    for name, campaign_, task_state, length, size in candidates:
        last = db.execute(
            'SELECT MAX(created_at) FROM files WHERE gone_at IS NULL'
            ' AND location = ?', (name.lstrip('/'),)).fetchone()[0]
        age = _seconds_since(last, now) if last else None
        if age is None or age <= stalled_seconds:
            continue
        found.append({'dataset': name, 'campaign': campaign_,
                      'last_arrival': last, 'quiet_age_s': round(age),
                      'task_state': task_state,
                      'files': int(length or 0), 'bytes': int(size or 0)})
    found.sort(key=lambda r: (r['last_arrival'] or '', r['dataset']))
    return len(found), found[offset:offset + limit], {
        'threshold_hours': stalled_hours}


_LISTINGS = {'ghosts': _ghosts, 'stuck_rules': _stuck_rules,
             'stalled_datasets': _stalled_datasets}


def listing(kind='ghosts', rse='', campaign='', state='', limit=None,
            offset=None, db_path=DEFAULT_DB):
    """One exception listing from the store.

    Returns a document with the listing name, the filters applied, the
    ``as_of`` block (last completed pass, pass in progress), ``total``,
    ``rows`` oldest first, ``next_offset`` when more rows follow, and for
    ghosts the ``by_rse`` account of the filtered population. A store
    that cannot be read returns an ``error`` field in the same envelope;
    nothing raises into a caller's page.
    """
    kind = str(kind or 'ghosts')
    limit, offset = _clamp(limit, offset)
    filters = {'rse': rse or '', 'campaign': campaign or '',
               'state': state or ''}
    envelope = {'listing': kind, 'filters': filters,
                'limit': limit, 'offset': offset}
    if kind not in _LISTINGS:
        envelope['error'] = (f'unknown listing {kind!r}; one of '
                             + ', '.join(LISTINGS))
        return envelope
    try:
        db = _open_readonly(db_path)
    except sqlite3.Error as exc:
        logger.error('storage store %s cannot be opened: %s', db_path, exc)
        envelope['error'] = f'storage store cannot be opened: {exc}'
        return envelope
    try:
        now = _now()
        envelope['as_of'] = _as_of(db)
        total, rows, extra = _LISTINGS[kind](
            db, now, rse or '', campaign or '', state or '', limit, offset,
            db_path)
    except sqlite3.Error as exc:
        logger.error('storage listing %s failed: %s', kind, exc)
        envelope['error'] = f'storage listing failed: {exc}'
        return envelope
    finally:
        db.close()
    envelope.update(extra)
    envelope['total'] = int(total)
    envelope['rows'] = rows
    envelope['returned'] = len(rows)
    envelope['next_offset'] = (offset + limit
                               if offset + len(rows) < total else None)
    envelope['as_of']['served_at'] = now.isoformat(timespec='seconds')
    return envelope
