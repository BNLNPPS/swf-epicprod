"""The storage pass: placement state of production data on every JLab
Rucio Storage Element (RSE), kept in a local store and projected as the
epicprod ``storage`` Snapper component (docs/STORAGE.md).

Three tiers per pass: RSE usage and account limits; every covered
dataset's per-RSE replica summary, rules and content; every covered
file's replica states in all states. Three modes: ``census`` (every
file under the production roots, once), ``full`` (nightly: every
dataset, the target campaigns' files), ``incremental`` (hourly: files
registered since the previous pass and files holding a non-available
replica). Transitions are derived by comparing a file's replicas with
its stored rows, so arrivals, completed transfers, deletions and ghost
appearance and clearance accrue as monotonic counters that every
consumer differences. ``projection()`` builds the bounded component
data from the store; publication belongs to the swf-monitor
maintainer (``monitor_app/snapper_storage.py``).

Rucio keeps no replica-state history, so every transition is observed
at pass cadence and a copying replica's age is measured from its DID's
creation time, an upper bound.
"""

import datetime as dt
import json
import os
import sqlite3
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor

DEFAULT_DB = '/data/wenauseic/swf-delivery/storage.sqlite'
ROOTS = ('/RECO', '/FULL', '/EVGEN', '/SIMU')
SCOPE = 'epic'
PRODUCTION_ACCOUNT = 'eicprod'
REPLICA_STATES = ('AVAILABLE', 'COPYING', 'UNAVAILABLE', 'BAD',
                  'TEMPORARY_UNAVAILABLE', 'BEING_DELETED')
# A registered file with no replica row at all is attributed here.
NO_RSE = 'none'
FILE_BATCH = 1000
META_CHUNK = 500
THREADS = 4
LISTING_HEAD = 50
MAX_RSES = 16
MAX_CAMPAIGNS = 8
MAX_SITES = 32
MAX_SERIALIZED_BYTES = 64 * 1024
RUCIO_TIME = '%a, %d %b %Y %H:%M:%S %Z'
FINAL_TASK_STATES = ('done', 'finished', 'failed', 'broken', 'aborted',
                     'exhausted', 'passed')
# Operator-visible thresholds, seeded at first read (SysConfig sets
# things; no knob hides behind a code default).
THRESHOLD_DEFAULTS = {
    'storage_copying_stuck_hours': 24,
    'storage_stalled_hours': 12,
    'storage_single_copy_warn_days': 7,
}

_print_lock = threading.Lock()


def log(message):
    with _print_lock:
        print(f'[{dt.datetime.now():%H:%M:%S}] {message}', flush=True)


def _iso(value):
    if value.tzinfo is not None:
        value = value.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return value.isoformat(timespec='seconds') + 'Z'


def _parse_iso(text):
    if not text:
        return None
    return dt.datetime.fromisoformat(text.rstrip('Z')).replace(
        tzinfo=dt.timezone.utc)


def _parse_rucio_time(text):
    """Rucio's HTTP-date form, UTC, to an ISO string; None when absent."""
    if not text:
        return None
    try:
        return _iso(dt.datetime.strptime(text, RUCIO_TIME)
                    .replace(tzinfo=dt.timezone.utc))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------

SCHEMA = (
    'CREATE TABLE IF NOT EXISTS files ('
    ' name TEXT PRIMARY KEY, campaign TEXT, root TEXT, location TEXT,'
    ' bytes INTEGER, created_at TEXT, events INTEGER, attached INTEGER,'
    ' first_seen TEXT, last_checked TEXT, gone_at TEXT)',
    'CREATE INDEX IF NOT EXISTS files_campaign ON files(campaign, gone_at)',
    'CREATE INDEX IF NOT EXISTS files_location ON files(location)',
    'CREATE TABLE IF NOT EXISTS replicas ('
    ' name TEXT, rse TEXT, state TEXT, first_available TEXT,'
    ' PRIMARY KEY (name, rse))',
    'CREATE INDEX IF NOT EXISTS replicas_rse_state ON replicas(rse, state)',
    'CREATE TABLE IF NOT EXISTS datasets ('
    ' name TEXT PRIMARY KEY, campaign TEXT, root TEXT, is_open INTEGER,'
    ' created_at TEXT, summary TEXT, rules TEXT, length INTEGER,'
    ' bytes INTEGER, task_state TEXT, last_checked TEXT, gone_at TEXT)',
    'CREATE INDEX IF NOT EXISTS datasets_campaign ON datasets(campaign)',
    'CREATE TABLE IF NOT EXISTS rses ('
    ' rse TEXT PRIMARY KEY, rse_type TEXT, used INTEGER, total INTEGER,'
    ' files INTEGER, usage_at TEXT, account_limit INTEGER,'
    ' last_checked TEXT)',
    'CREATE TABLE IF NOT EXISTS counters ('
    ' key TEXT PRIMARY KEY, value INTEGER NOT NULL)',
    'CREATE TABLE IF NOT EXISTS passes ('
    ' id INTEGER PRIMARY KEY AUTOINCREMENT, mode TEXT, started TEXT,'
    ' finished TEXT, campaigns TEXT, files_checked INTEGER,'
    ' datasets_checked INTEGER, errors TEXT)',
    'CREATE TABLE IF NOT EXISTS latencies ('
    ' pass_id INTEGER, campaign TEXT, kind TEXT, seconds REAL)',
    'CREATE INDEX IF NOT EXISTS latencies_pass ON latencies(pass_id)',
)


def open_store(path=DEFAULT_DB):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    db = sqlite3.connect(path, check_same_thread=False)
    db.execute('PRAGMA journal_mode=WAL')
    for statement in SCHEMA:
        db.execute(statement)
    db.commit()
    return db


def _bump(counters, key, n=1):
    if n:
        counters[key] = counters.get(key, 0) + n


def _flush_counters(db, counters):
    db.executemany(
        'INSERT INTO counters (key, value) VALUES (?, ?)'
        ' ON CONFLICT(key) DO UPDATE SET value = value + excluded.value',
        [(key, int(n)) for key, n in counters.items() if n])


def counter_values(db):
    return {key: int(value) for key, value in
            db.execute('SELECT key, value FROM counters')}


# ---------------------------------------------------------------------------
# The catalog
# ---------------------------------------------------------------------------

class Catalog:
    """JLab Rucio reads, on the helpers the catalog services use."""

    def __init__(self):
        from pcs.services import (_jlab_rucio_auth, _jlab_rucio_get,
                                  _jlab_rucio_post, _ndjson)
        self.token = _jlab_rucio_auth()
        self._get = _jlab_rucio_get
        self._post = _jlab_rucio_post
        self._ndjson = _ndjson
        self.errors = []
        self._lock = threading.Lock()

    def _fail(self, what, exc):
        with self._lock:
            self.errors.append(f'{what}: {exc}')
        log(f'ERROR {what}: {exc}')

    def search(self, pattern, did_type, created_after=None):
        """DID names matching a name pattern; a failed search raises,
        since a missing inventory must not read as deletions."""
        params = {'type': did_type, 'name': pattern}
        if created_after:
            params['created_after'] = created_after
        rows = self._ndjson(self._get(
            f'/dids/{SCOPE}/dids/search', self.token, timeout=300, **params))
        return [row for row in rows if isinstance(row, str)]

    def bulkmeta(self, names):
        out = {}
        for start in range(0, len(names), META_CHUNK):
            chunk = names[start:start + META_CHUNK]
            try:
                rows = self._ndjson(self._post(
                    '/dids/bulkmeta', self.token,
                    {'dids': [{'scope': SCOPE, 'name': n} for n in chunk]}))
            except Exception as exc:                          # noqa: BLE001
                self._fail(f'bulkmeta {len(chunk)} dids', exc)
                continue
            for row in rows:
                if isinstance(row, dict) and row.get('name'):
                    out[row['name']] = row
        return out

    def replicas(self, names):
        """{name: {rse: state}} for file DIDs, all states, plus bytes
        under the key ``_bytes``. A name the listing does not return
        has no replica row and maps to {}."""
        out = {n: {} for n in names}
        for start in range(0, len(names), FILE_BATCH):
            batch = names[start:start + FILE_BATCH]
            try:
                rows = self._ndjson(self._post(
                    '/replicas/list', self.token,
                    {'dids': [{'scope': SCOPE, 'name': n} for n in batch],
                     'all_states': True, 'ignore_availability': True}))
            except Exception as exc:                          # noqa: BLE001
                self._fail(f'replicas {len(batch)} dids', exc)
                for n in batch:
                    out.pop(n, None)
                continue
            for row in rows:
                if not isinstance(row, dict) or not row.get('name'):
                    continue
                states = {str(rse): str(state) for rse, state
                          in (row.get('states') or {}).items()}
                states['_bytes'] = int(row.get('bytes') or 0)
                out[row['name']] = states
        return out

    def dataset_summary(self, name):
        rows = self._ndjson(self._get(
            f'/replicas/{SCOPE}/{name}/datasets', self.token))
        return [{'rse': r.get('rse'), 'length': r.get('length'),
                 'available_length': r.get('available_length'),
                 'bytes': r.get('bytes'),
                 'available_bytes': r.get('available_bytes'),
                 'state': r.get('state'),
                 'created_at': _parse_rucio_time(r.get('created_at')),
                 'updated_at': _parse_rucio_time(r.get('updated_at'))}
                for r in rows if isinstance(r, dict)]

    def dataset_rules(self, name):
        rows = self._ndjson(self._get(
            f'/dids/{SCOPE}{name}/rules', self.token))
        return [{'id': r.get('id'), 'state': r.get('state'),
                 'rse_expression': r.get('rse_expression'),
                 'copies': r.get('copies'),
                 'locks_ok': int(r.get('locks_ok_cnt') or 0),
                 'locks_replicating': int(r.get('locks_replicating_cnt') or 0),
                 'locks_stuck': int(r.get('locks_stuck_cnt') or 0),
                 'created_at': _parse_rucio_time(r.get('created_at')),
                 'stuck_at': _parse_rucio_time(r.get('stuck_at')),
                 'expires_at': _parse_rucio_time(r.get('expires_at')),
                 'subscription': bool(r.get('subscription_id'))}
                for r in rows if isinstance(r, dict)]

    def dataset_content(self, name):
        rows = self._ndjson(self._get(
            f'/dids/{SCOPE}{name}/dids', self.token, timeout=300))
        return [r['name'] for r in rows
                if isinstance(r, dict) and r.get('name')]

    def rses(self):
        rows = self._ndjson(self._get('/rses/', self.token))
        return {r['rse']: str(r.get('rse_type') or '')
                for r in rows if isinstance(r, dict) and r.get('rse')}

    def rse_usage(self, rse):
        rows = self._ndjson(self._get(f'/rses/{rse}/usage', self.token))
        for r in rows:
            if isinstance(r, dict) and r.get('source') == 'rucio':
                return r
        return rows[0] if rows and isinstance(rows[0], dict) else {}

    def account_limits(self):
        text = self._get(f'/accounts/{PRODUCTION_ACCOUNT}/limits/local',
                         self.token)
        limits = json.loads(text.replace('Infinity', 'null'))
        return {rse: (int(v) if v is not None else None)
                for rse, v in limits.items()}


# ---------------------------------------------------------------------------
# Campaign and location vocabulary
# ---------------------------------------------------------------------------

def _split(name):
    """(root, campaign family, location) of a DID under the roots;
    (None, None, None) for a name outside them."""
    from pcs.services import campaign_family

    segs = name.split('/')
    if len(segs) < 3 or not segs[1] or not segs[2]:
        return None, None, None
    root = segs[1]
    if root == 'EVGEN':
        # EVGEN DIDs carry no campaign; the family is the physics
        # segment so EVGEN inventory groups by its own vocabulary.
        return root, 'EVGEN', '/'.join(segs[1:-1])
    return root, campaign_family(segs[2]), '/'.join(segs[1:-1])


def target_campaigns():
    from .delivery_daily import target_campaigns as delivery_targets
    return tuple(delivery_targets())


def thresholds():
    """The SysConfig thresholds, seeded at their defaults."""
    try:
        from monitor_app.models import SysConfig
        return {key: float(SysConfig.get_setting(key, default))
                for key, default in THRESHOLD_DEFAULTS.items()}
    except Exception as exc:                                  # noqa: BLE001
        log(f'ERROR thresholds: {exc}; using defaults')
        return dict(THRESHOLD_DEFAULTS)


# ---------------------------------------------------------------------------
# RSE tier
# ---------------------------------------------------------------------------

def rse_tier(db, catalog, now):
    """Upsert every RSE's type, usage and account limit; returns
    {rse: type}."""
    types = catalog.rses()
    try:
        limits = catalog.account_limits()
    except Exception as exc:                                  # noqa: BLE001
        catalog._fail('account limits', exc)
        limits = {}
    stamp = _iso(now)
    for rse, rse_type in sorted(types.items()):
        usage = {}
        try:
            usage = catalog.rse_usage(rse) or {}
        except Exception as exc:                              # noqa: BLE001
            catalog._fail(f'usage {rse}', exc)
        db.execute(
            'INSERT INTO rses (rse, rse_type, used, total, files, usage_at,'
            ' account_limit, last_checked) VALUES (?,?,?,?,?,?,?,?)'
            ' ON CONFLICT(rse) DO UPDATE SET rse_type=excluded.rse_type,'
            ' used=excluded.used, total=excluded.total, files=excluded.files,'
            ' usage_at=excluded.usage_at, account_limit=excluded.account_limit,'
            ' last_checked=excluded.last_checked',
            (rse, rse_type, usage.get('used'), usage.get('total'),
             usage.get('files'), _parse_rucio_time(usage.get('updated_at')),
             limits.get(rse), stamp))
    db.commit()
    return types


# ---------------------------------------------------------------------------
# Dataset tier
# ---------------------------------------------------------------------------

def dataset_inventory(catalog):
    """Every dataset under the roots, as {name: (root, campaign)}."""
    out = {}
    for root in ROOTS:
        for name in catalog.search(root + '/*', 'dataset'):
            r, campaign, _ = _split(name)
            if r:
                out[name] = (r, campaign)
    return out


def _task_states(locations):
    """{location: 'active'|'final'|None} for dataset locations, through
    the task output records and the PanDA task status."""
    states = {}
    if not locations:
        return states
    try:
        from django.db import connections
        from monitor_app.panda.constants import PANDA_SCHEMA
        from pcs.models import ProdTask

        by_location = {}
        for task in ProdTask.objects.select_related('dataset'):
            for output in task.outputs:
                did = str(output.get('did') or '')
                path = did.split(':', 1)[-1].strip('/')
                if path in locations:
                    by_location.setdefault(path, set()).add(task.pk)
        task_ids = {pk for pks in by_location.values() for pk in pks}
        jedi_by_task = {}
        if task_ids:
            for pk, jedi in (ProdTask.objects.filter(pk__in=task_ids)
                             .values_list('pk', 'panda_task_id')):
                if jedi:
                    jedi_by_task[pk] = int(jedi)
        status_by_jedi = {}
        if jedi_by_task:
            ids = sorted(set(jedi_by_task.values()))
            placeholders = ', '.join(['%s'] * len(ids))
            with connections['panda'].cursor() as cursor:
                cursor.execute(
                    f'SELECT "jeditaskid", "status" FROM "{PANDA_SCHEMA}".'
                    f'"jedi_tasks" WHERE "jeditaskid" IN ({placeholders})',
                    ids)
                status_by_jedi = {int(j): str(s) for j, s in cursor.fetchall()}
        for location, pks in by_location.items():
            statuses = [status_by_jedi.get(jedi_by_task.get(pk))
                        for pk in pks]
            statuses = [s for s in statuses if s]
            if not statuses:
                states[location] = None
            elif all(s in FINAL_TASK_STATES for s in statuses):
                states[location] = 'final'
            else:
                states[location] = 'active'
    except Exception as exc:                                  # noqa: BLE001
        log(f'ERROR task states: {exc}')
    return states


def dataset_tier(db, catalog, now, selected, inventory):
    """Refresh the selected datasets' summary, rules and content;
    returns {dataset: set(content names)}."""
    stamp = _iso(now)
    meta = catalog.bulkmeta(sorted(selected))
    content = {}
    lock = threading.Lock()

    def one(name):
        try:
            summary = catalog.dataset_summary(name)
            rules = catalog.dataset_rules(name)
            names = catalog.dataset_content(name)
        except Exception as exc:                              # noqa: BLE001
            catalog._fail(f'dataset {name}', exc)
            return
        root, campaign = inventory.get(name, _split(name)[:2])
        m = meta.get(name) or {}
        length = max([int(s.get('length') or 0) for s in summary] + [0])
        size = max([int(s.get('bytes') or 0) for s in summary] + [0])
        with lock:
            content[name] = set(names)
            db.execute(
                'INSERT INTO datasets (name, campaign, root, is_open,'
                ' created_at, summary, rules, length, bytes, last_checked,'
                ' gone_at) VALUES (?,?,?,?,?,?,?,?,?,?,NULL)'
                ' ON CONFLICT(name) DO UPDATE SET campaign=excluded.campaign,'
                ' root=excluded.root, is_open=excluded.is_open,'
                ' created_at=excluded.created_at, summary=excluded.summary,'
                ' rules=excluded.rules, length=excluded.length,'
                ' bytes=excluded.bytes, last_checked=excluded.last_checked,'
                ' gone_at=NULL',
                (name, campaign, root,
                 1 if m.get('is_open') else 0,
                 _parse_rucio_time(m.get('created_at')),
                 json.dumps(summary, separators=(',', ':')),
                 json.dumps(rules, separators=(',', ':')),
                 length, size, stamp))

    with ThreadPoolExecutor(max_workers=THREADS) as pool:
        list(pool.map(one, sorted(selected)))
    db.commit()
    # Task state for the checked datasets, for the stalled reading.
    task_states = _task_states(set(content))
    db.executemany('UPDATE datasets SET task_state=? WHERE name=?',
                   [(state, name) for name, state in task_states.items()])
    db.commit()
    return content


def datasets_to_check(db, mode, inventory, campaigns, touched):
    """The dataset tier's selection: everything in census and full
    modes; in incremental mode the datasets touched by new files, plus
    those whose stored row is open, partially placed on every RSE, or
    carrying a non-OK rule."""
    if mode in ('census', 'full'):
        return set(inventory)
    selected = {name for name in inventory if name in touched}
    for name, is_open, summary, rules in db.execute(
            'SELECT name, is_open, summary, rules FROM datasets'
            ' WHERE gone_at IS NULL AND campaign IN (%s)'
            % ', '.join(['?'] * len(campaigns)), list(campaigns)):
        if name not in inventory:
            continue
        rows = json.loads(summary or '[]')
        rule_rows = json.loads(rules or '[]')
        complete_somewhere = any(
            (r.get('length') or 0) > 0
            and (r.get('available_length') or 0) >= (r.get('length') or 0)
            for r in rows)
        if is_open or not complete_somewhere or any(
                r.get('state') != 'OK' for r in rule_rows):
            selected.add(name)
    return selected


# ---------------------------------------------------------------------------
# File tier
# ---------------------------------------------------------------------------

def _search_files(catalog, mode, campaigns, since):
    """The file names to resolve this pass and the campaigns whose
    inventory was listed exhaustively (a stored file of those absent
    from the listing is gone)."""
    names = set()
    if mode == 'census':
        for root in ROOTS:
            names.update(catalog.search(root + '/*', 'file'))
        return names, None
    if mode == 'full':
        for root in ROOTS:
            if root == '/EVGEN':
                names.update(catalog.search(root + '/*', 'file'))
                continue
            for family in campaigns:
                if family == 'EVGEN':
                    continue
                names.update(catalog.search(f'{root}/{family}*', 'file'))
        return names, set(campaigns) | {'EVGEN'}
    created_after = since.astimezone(dt.timezone.utc).strftime(
        '%Y-%m-%dT%H:%M:%S')
    for root in ROOTS:
        names.update(catalog.search(root + '/*', 'file',
                                    created_after=created_after))
    return names, set()


def _standing_names(db, campaigns):
    """Stored files holding any non-available replica or none at all,
    in the covered campaigns: the set an incremental pass rechecks."""
    marks = ', '.join(['?'] * len(campaigns))
    out = set()
    for (name,) in db.execute(
            'SELECT f.name FROM files f WHERE f.gone_at IS NULL'
            f' AND f.campaign IN ({marks}) AND NOT EXISTS'
            ' (SELECT 1 FROM replicas r WHERE r.name = f.name'
            "  AND r.state = 'AVAILABLE')", list(campaigns)):
        out.add(name)
    for (name,) in db.execute(
            'SELECT DISTINCT r.name FROM replicas r JOIN files f'
            ' ON f.name = r.name WHERE f.gone_at IS NULL'
            f" AND f.campaign IN ({marks}) AND r.state != 'AVAILABLE'",
            list(campaigns)):
        out.add(name)
    return out


def _stored(db, names):
    """Stored rows for names: {name: file row dict},
    {name: {rse: (state, first_available)}}."""
    files, reps = {}, {}
    ordered = sorted(names)
    for start in range(0, len(ordered), 900):
        chunk = ordered[start:start + 900]
        marks = ', '.join(['?'] * len(chunk))
        for row in db.execute(
                'SELECT name, campaign, root, location, bytes, created_at,'
                ' events, attached, first_seen, gone_at FROM files'
                f' WHERE name IN ({marks})', chunk):
            files[row[0]] = {
                'campaign': row[1], 'root': row[2], 'location': row[3],
                'bytes': row[4], 'created_at': row[5], 'events': row[6],
                'attached': row[7], 'first_seen': row[8], 'gone_at': row[9]}
        for name, rse, state, first in db.execute(
                'SELECT name, rse, state, first_available FROM replicas'
                f' WHERE name IN ({marks})', chunk):
            reps.setdefault(name, {})[rse] = (state, first)
    return files, reps


def _first_rse_by_location(db):
    """location -> the RSE whose dataset replica was created first;
    the first-copy attribution for a file first seen with several
    available replicas."""
    out = {}
    for name, summary in db.execute(
            'SELECT name, summary FROM datasets WHERE gone_at IS NULL'):
        rows = [r for r in json.loads(summary or '[]') if r.get('created_at')]
        if rows:
            out[name.lstrip('/')] = min(rows, key=lambda r: r['created_at'])['rse']
    return out


def _seconds_since(stamp, now):
    then = _parse_iso(stamp)
    return (now - then).total_seconds() if then else None


class _Ledger:
    """Counter and latency accrual for one pass."""

    def __init__(self, now, since, tape_rses, first_rse):
        self.now = now
        self.since = since
        self.stamp = _iso(now)
        self.tape = set(tape_rses)
        self.first_rse = first_rse
        self.counters = {}
        self.latencies = []

    def _c(self, key, n=1):
        _bump(self.counters, key, n)

    def _latency(self, campaign, kind, seconds):
        if seconds is not None and seconds >= 0:
            self.latencies.append((campaign, kind, float(seconds)))

    def first_sight(self, name, campaign, location, created, size, states):
        """A file with no stored row: the census, or a file registered
        since the previous pass. Returns the replica rows to store."""
        available = sorted(r for r, s in states.items() if s == 'AVAILABLE')
        rows = {}
        if available:
            first = (available[0] if len(available) == 1
                     else (self.first_rse.get(location)
                           if self.first_rse.get(location) in available
                           else available[0]))
            for rse in available:
                self._c(f'rse:{rse}:arrived_files')
                self._c(f'rse:{rse}:arrived_bytes', size)
                kind = 'first_copy' if rse == first else 'replica'
                self._c(f'rse:{rse}:{kind}_files')
                self._c(f'rse:{rse}:{kind}_bytes', size)
            self._c(f'campaign:{campaign}:arrived_files')
            self._c(f'campaign:{campaign}:arrived_bytes', size)
            if any(rse in self.tape for rse in available):
                self._c(f'campaign:{campaign}:archived_files')
                self._c(f'campaign:{campaign}:archived_bytes', size)
            # Registered since the previous pass and already available:
            # the interval's arrival, with its latency an upper bound.
            recent = created and _parse_iso(created) and (
                _parse_iso(created) > self.since)
            first_available = self.stamp if recent else created
            if recent:
                self._latency(campaign, 'registration_to_available',
                              _seconds_since(created, self.now))
            for rse, state in states.items():
                rows[rse] = (state, first_available if state == 'AVAILABLE'
                             else None)
        else:
            holders = [r for r in states] or [NO_RSE]
            for rse in holders:
                self._c(f'rse:{rse}:ghosts_appeared')
            for rse, state in states.items():
                rows[rse] = (state, None)
        return rows

    def transition(self, name, campaign, location, created, size,
                   old_reps, states):
        """A stored file re-resolved. Returns the replica rows to store."""
        old_avail = {r for r, (s, _) in old_reps.items() if s == 'AVAILABLE'}
        ever_avail = {r for r, (_, first) in old_reps.items() if first}
        new_avail = {r for r, s in states.items() if s == 'AVAILABLE'}
        earliest_old = min(
            (first for _, first in old_reps.values() if first), default=None)
        earliest_old_disk = min(
            (first for r, (_, first) in old_reps.items()
             if first and r not in self.tape), default=None)
        rows = {}
        for rse, state in states.items():
            old_state, old_first = old_reps.get(rse, (None, None))
            first = old_first
            if state == 'AVAILABLE' and rse not in ever_avail:
                first = self.stamp
                self._c(f'rse:{rse}:arrived_files')
                self._c(f'rse:{rse}:arrived_bytes', size)
                if not old_avail:
                    self._c(f'rse:{rse}:first_copy_files')
                    self._c(f'rse:{rse}:first_copy_bytes', size)
                    self._c(f'campaign:{campaign}:arrived_files')
                    self._c(f'campaign:{campaign}:arrived_bytes', size)
                    self._latency(campaign, 'registration_to_available',
                                  _seconds_since(created, self.now))
                    old_avail = {rse}
                else:
                    self._c(f'rse:{rse}:replica_files')
                    self._c(f'rse:{rse}:replica_bytes', size)
                    self._latency(campaign, 'first_to_second_copy',
                                  _seconds_since(earliest_old, self.now))
                if old_state == 'COPYING':
                    self._c(f'rse:{rse}:transfers_completed')
                if rse in self.tape and not any(
                        r in self.tape for r in ever_avail):
                    self._c(f'campaign:{campaign}:archived_files')
                    self._c(f'campaign:{campaign}:archived_bytes', size)
                    self._latency(campaign, 'disk_to_tape',
                                  _seconds_since(earliest_old_disk, self.now))
            if state == 'BAD' and old_state != 'BAD':
                self._c(f'rse:{rse}:bad_appeared')
            rows[rse] = (state, first)
        for rse in old_reps:
            if rse not in states:
                self._c(f'rse:{rse}:deleted_files')
                self._c(f'rse:{rse}:deleted_bytes', size)
        old_ghost = not {r for r, (s, _) in old_reps.items()
                         if s == 'AVAILABLE'}
        new_ghost = not new_avail
        if new_ghost and not old_ghost:
            for rse in ([r for r in states] or [NO_RSE]):
                self._c(f'rse:{rse}:ghosts_appeared')
        elif old_ghost and not new_ghost:
            for rse in ([r for r in old_reps] or [NO_RSE]):
                self._c(f'rse:{rse}:ghosts_cleared')
        return rows

    def gone(self, name, size, old_reps):
        """A stored file absent from an exhaustive listing."""
        for rse in old_reps:
            self._c(f'rse:{rse}:deleted_files')
            self._c(f'rse:{rse}:deleted_bytes', size)
        if not any(s == 'AVAILABLE' for s, _ in old_reps.values()):
            for rse in ([r for r in old_reps] or [NO_RSE]):
                self._c(f'rse:{rse}:ghosts_cleared')


def file_tier(db, catalog, now, since, mode, campaigns, names, exhaustive,
              content, tape_rses, pass_id, limit_files=0):
    """Resolve the named files' replicas, apply transitions against
    the store, mark files gone under an exhaustive listing, and accrue
    counters and latencies. Returns the number of files resolved."""
    stamp = _iso(now)
    ordered = sorted(names)
    if limit_files:
        ordered = ordered[:int(limit_files)]
        log(f'capped at {len(ordered)} of {len(names)} files (validation run)')
    attached = {}
    for members in content.values():
        for member in members:
            attached[member] = 1
    listed_datasets = {'/' + n.lstrip('/') for n in content}
    ledger = _Ledger(now, since, tape_rses, _first_rse_by_location(db))
    resolved = 0
    for start in range(0, len(ordered), 20000):
        chunk = ordered[start:start + 20000]
        stored_files, stored_reps = _stored(db, chunk)
        new_names = [n for n in chunk if n not in stored_files]
        meta = catalog.bulkmeta(new_names) if new_names else {}
        batches = [chunk[i:i + FILE_BATCH]
                   for i in range(0, len(chunk), FILE_BATCH)]
        replica_map = {}
        with ThreadPoolExecutor(max_workers=THREADS) as pool:
            for result in pool.map(catalog.replicas, batches):
                replica_map.update(result)
        file_rows, replica_rows, drop_names = [], [], []
        for name in chunk:
            states = replica_map.get(name)
            if states is None:
                continue
            resolved += 1
            listed_bytes = states.pop('_bytes', 0)
            root, campaign, location = _split(name)
            if root is None:
                continue
            old = stored_files.get(name)
            if old is None:
                m = meta.get(name) or {}
                created = _parse_rucio_time(m.get('created_at')) or stamp
                events = m.get('events')
                size = int(listed_bytes or m.get('bytes') or 0)
                rows = ledger.first_sight(
                    name, campaign, location, created, size, states)
                first_seen = stamp
            else:
                created = old['created_at']
                events = old['events']
                size = int(listed_bytes or old['bytes'] or 0)
                rows = ledger.transition(
                    name, campaign, location, created, size,
                    stored_reps.get(name, {}), states)
                first_seen = old['first_seen'] or stamp
            dataset = '/' + location
            if dataset in listed_datasets:
                is_attached = attached.get(name, 0)
            else:
                is_attached = old['attached'] if old else None
            file_rows.append((name, campaign, root, location, size, created,
                              events, is_attached, first_seen, stamp))
            drop_names.append((name,))
            for rse, (state, first) in rows.items():
                replica_rows.append((name, rse, state, first))
        db.executemany(
            'INSERT INTO files (name, campaign, root, location, bytes,'
            ' created_at, events, attached, first_seen, last_checked,'
            ' gone_at) VALUES (?,?,?,?,?,?,?,?,?,?,NULL)'
            ' ON CONFLICT(name) DO UPDATE SET campaign=excluded.campaign,'
            ' root=excluded.root, location=excluded.location,'
            ' bytes=excluded.bytes, created_at=excluded.created_at,'
            ' events=excluded.events, attached=excluded.attached,'
            ' last_checked=excluded.last_checked, gone_at=NULL',
            file_rows)
        db.executemany('DELETE FROM replicas WHERE name = ?', drop_names)
        db.executemany(
            'INSERT INTO replicas (name, rse, state, first_available)'
            ' VALUES (?,?,?,?)', replica_rows)
        db.commit()
        log(f'  files {min(start + 20000, len(ordered))}/{len(ordered)}')
    # Gone: stored files of exhaustively listed campaigns absent from
    # the listing. A census lists every root, so every stored file of
    # every campaign is subject; a full pass only the target campaigns.
    if exhaustive is None or exhaustive:
        listed = set(ordered) if not limit_files else None
        if listed is not None:
            query = ('SELECT name, bytes, campaign FROM files'
                     ' WHERE gone_at IS NULL')
            params = []
            if exhaustive:
                query += ' AND campaign IN (%s)' % ', '.join(
                    ['?'] * len(exhaustive))
                params = sorted(exhaustive)
            gone = [(n, b) for n, b, _ in db.execute(query, params)
                    if n not in listed]
            if gone:
                _, gone_reps = _stored(db, [n for n, _ in gone])
                for name, size in gone:
                    ledger.gone(name, int(size or 0), gone_reps.get(name, {}))
                db.executemany('UPDATE files SET gone_at = ? WHERE name = ?',
                               [(stamp, n) for n, _ in gone])
                db.executemany('DELETE FROM replicas WHERE name = ?',
                               [(n,) for n, _ in gone])
                log(f'  {len(gone)} files gone')
    _flush_counters(db, ledger.counters)
    db.executemany(
        'INSERT INTO latencies (pass_id, campaign, kind, seconds)'
        ' VALUES (?,?,?,?)',
        [(pass_id, c, k, s) for c, k, s in ledger.latencies])
    db.commit()
    return resolved


# ---------------------------------------------------------------------------
# The projection
# ---------------------------------------------------------------------------

def _fold_campaigns(values, campaigns):
    """Keep the target campaigns' entries, fold the rest into 'other'."""
    out = {}
    other = {}
    for campaign, block in values.items():
        if campaign in campaigns:
            out[campaign] = block
        else:
            for key, n in block.items():
                other[key] = other.get(key, 0) + n
    if other:
        out['other'] = other
    return out


def _quantiles(values):
    if not values:
        return {'n': 0}
    values = sorted(values)
    p90 = values[min(len(values) - 1, int(round(0.9 * (len(values) - 1))))]
    return {'n': len(values), 'median': round(statistics.median(values)),
            'p90': round(p90), 'max': round(values[-1])}


def _stats_block(rows):
    """{key: {'files': n, 'bytes': b}} from (key, files, bytes) rows."""
    return {str(k): {'files': int(f or 0), 'bytes': int(b or 0)}
            for k, f, b in rows}


def projection(db, now, since, campaigns, pass_info):
    """The bounded component data from the store (docs/STORAGE.md,
    The storage component)."""
    th = thresholds()
    stuck_seconds = th['storage_copying_stuck_hours'] * 3600
    stalled_seconds = th['storage_stalled_hours'] * 3600
    single_copy_seconds = th['storage_single_copy_warn_days'] * 86400
    counters = counter_values(db)
    campaigns = tuple(campaigns)[:MAX_CAMPAIGNS]
    stamp = _iso(now)

    rse_rows = list(db.execute(
        'SELECT rse, rse_type, used, total, files, usage_at, account_limit'
        ' FROM rses ORDER BY rse'))[:MAX_RSES]
    tape = {r[0] for r in rse_rows if r[1] == 'TAPE'}
    verdicts = {}

    # Ghost files: registered, no available replica anywhere.
    ghost_files = {}
    for name, campaign, size, created in db.execute(
            'SELECT f.name, f.campaign, f.bytes, f.created_at FROM files f'
            ' WHERE f.gone_at IS NULL AND NOT EXISTS'
            ' (SELECT 1 FROM replicas r WHERE r.name = f.name'
            "  AND r.state = 'AVAILABLE')"):
        ghost_files[name] = {'campaign': campaign, 'bytes': int(size or 0),
                             'created_at': created, 'holders': {}}
    if ghost_files:
        ordered = sorted(ghost_files)
        for start in range(0, len(ordered), 900):
            chunk = ordered[start:start + 900]
            marks = ', '.join(['?'] * len(chunk))
            for name, rse, state in db.execute(
                    'SELECT name, rse, state FROM replicas'
                    f' WHERE name IN ({marks})', chunk):
                ghost_files[name]['holders'][rse] = state
    ghosts_by_rse = {}
    for name, g in ghost_files.items():
        holders = g['holders'] or {NO_RSE: 'none'}
        for rse, state in holders.items():
            slot = ghosts_by_rse.setdefault(
                rse, {'files': 0, 'bytes': 0, 'by_state': {},
                      'by_campaign': {}, 'oldest': None, 'rows': []})
            slot['files'] += 1
            slot['bytes'] += g['bytes']
            slot['by_state'][state] = slot['by_state'].get(state, 0) + 1
            slot['by_campaign'][g['campaign']] = (
                slot['by_campaign'].get(g['campaign'], 0) + 1)
            if g['created_at'] and (slot['oldest'] is None
                                    or g['created_at'] < slot['oldest']):
                slot['oldest'] = g['created_at']
            slot['rows'].append((g['created_at'] or '', name, rse, state,
                                 g['campaign'], g['bytes']))

    # Dataset summaries and rules, per RSE.
    dataset_rows = list(db.execute(
        'SELECT name, campaign, root, is_open, summary, rules, length,'
        ' task_state FROM datasets WHERE gone_at IS NULL'))
    last_registered = {loc: stamp_ for loc, stamp_ in db.execute(
        'SELECT location, MAX(created_at) FROM files WHERE gone_at IS NULL'
        ' GROUP BY location')}
    ds_by_rse = {}
    rules_by_rse = {}
    stuck_rows = []
    for name, campaign, root, is_open, summary, rules, length, task_state \
            in dataset_rows:
        for s in json.loads(summary or '[]'):
            rse = s.get('rse')
            slot = ds_by_rse.setdefault(
                rse, {'total': 0, 'complete': 0, 'partial': 0, 'empty': 0,
                      'unavailable': 0})
            slot['total'] += 1
            n, a = int(s.get('length') or 0), int(s.get('available_length') or 0)
            if n == 0:
                slot['empty'] += 1
            elif a >= n:
                slot['complete'] += 1
            elif a == 0:
                slot['unavailable'] += 1
            else:
                slot['partial'] += 1
        for rule in json.loads(rules or '[]'):
            expression = str(rule.get('rse_expression') or '')
            holders = [r[0] for r in rse_rows if r[0] in expression] or [expression]
            for rse in holders:
                slot = rules_by_rse.setdefault(
                    rse, {'by_state': {}, 'locks': {'ok': 0, 'replicating': 0,
                                                    'stuck': 0},
                          'oldest_stuck_age_s': None, 'expiring_30d': 0})
                state = str(rule.get('state') or 'UNKNOWN')
                slot['by_state'][state] = slot['by_state'].get(state, 0) + 1
                slot['locks']['ok'] += rule.get('locks_ok') or 0
                slot['locks']['replicating'] += rule.get('locks_replicating') or 0
                slot['locks']['stuck'] += rule.get('locks_stuck') or 0
                if rule.get('stuck_at'):
                    age = _seconds_since(rule['stuck_at'], now)
                    if age is not None and (slot['oldest_stuck_age_s'] is None
                                            or age > slot['oldest_stuck_age_s']):
                        slot['oldest_stuck_age_s'] = round(age)
                    stuck_rows.append((rule['stuck_at'], name, rse,
                                       int(rule.get('locks_stuck') or 0)))
                expires = _parse_iso(rule.get('expires_at'))
                if expires and (expires - now).total_seconds() < 30 * 86400:
                    slot['expiring_30d'] += 1

    rses = {}
    for rse, rse_type, used, total, files, usage_at, limit in rse_rows:
        inventory = _stats_block(db.execute(
            'SELECT r.state, COUNT(*), SUM(f.bytes) FROM replicas r'
            ' JOIN files f ON f.name = r.name WHERE f.gone_at IS NULL'
            ' AND r.rse = ? GROUP BY r.state', (rse,)))
        by_campaign = {}
        for campaign, state, n, b in db.execute(
                'SELECT f.campaign, r.state, COUNT(*), SUM(f.bytes)'
                ' FROM replicas r JOIN files f ON f.name = r.name'
                ' WHERE f.gone_at IS NULL AND r.rse = ?'
                ' GROUP BY f.campaign, r.state', (rse,)):
            block = by_campaign.setdefault(str(campaign), {})
            block[f'files_{state}'] = block.get(f'files_{state}', 0) + int(n)
            block[f'bytes_{state}'] = block.get(f'bytes_{state}', 0) + int(b or 0)
        by_root = {}
        for root, state, n, b in db.execute(
                'SELECT f.root, r.state, COUNT(*), SUM(f.bytes)'
                ' FROM replicas r JOIN files f ON f.name = r.name'
                ' WHERE f.gone_at IS NULL AND r.rse = ?'
                ' GROUP BY f.root, r.state', (rse,)):
            block = by_root.setdefault(str(root), {})
            block[f'files_{state}'] = block.get(f'files_{state}', 0) + int(n)
            block[f'bytes_{state}'] = block.get(f'bytes_{state}', 0) + int(b or 0)
        copying = [(size, created) for size, created in db.execute(
            'SELECT f.bytes, f.created_at FROM replicas r JOIN files f'
            ' ON f.name = r.name WHERE f.gone_at IS NULL AND r.rse = ?'
            " AND r.state = 'COPYING'", (rse,))]
        ages = [a for a in (_seconds_since(c, now) for _, c in copying)
                if a is not None]
        backlog = {'copying_files': len(copying),
                   'copying_bytes': sum(int(s or 0) for s, _ in copying),
                   'age_s': _quantiles(ages),
                   'over_threshold': sum(1 for a in ages if a > stuck_seconds)}
        ghosts = ghosts_by_rse.get(rse) or {}
        rses[rse] = {
            'type': rse_type,
            'capacity': {
                'used': used, 'total': total, 'files': files,
                'limit': limit,
                'fraction': (round(used / limit, 4)
                             if used and limit else None),
                'as_of': usage_at},
            'inventory': {'by_state': inventory,
                          'by_campaign': _fold_campaigns(by_campaign, campaigns),
                          'by_root': by_root},
            'datasets': ds_by_rse.get(rse) or {
                'total': 0, 'complete': 0, 'partial': 0, 'empty': 0,
                'unavailable': 0},
            'rules': rules_by_rse.get(rse) or {
                'by_state': {}, 'locks': {'ok': 0, 'replicating': 0, 'stuck': 0},
                'oldest_stuck_age_s': None, 'expiring_30d': 0},
            'backlog': backlog,
            'ghosts': {'files': ghosts.get('files', 0),
                       'bytes': ghosts.get('bytes', 0),
                       'by_state': ghosts.get('by_state', {}),
                       'by_campaign': _fold_campaigns(
                           {c: {'files': n} for c, n
                            in ghosts.get('by_campaign', {}).items()},
                           campaigns),
                       'oldest_age_s': (round(_seconds_since(ghosts['oldest'], now))
                                        if ghosts.get('oldest') else None)},
            'flow': {key: counters.get(f'rse:{rse}:{key}', 0) for key in (
                'arrived_files', 'arrived_bytes', 'first_copy_files',
                'first_copy_bytes', 'replica_files', 'replica_bytes',
                'transfers_completed', 'deleted_files', 'deleted_bytes',
                'ghosts_appeared', 'ghosts_cleared', 'bad_appeared')},
        }
        if backlog['over_threshold']:
            verdicts[f'rse:{rse}:transfers_stuck'] = 'warning'
        if rses[rse]['rules']['locks']['stuck']:
            verdicts[f'rse:{rse}:rules_stuck'] = 'warning'
    if NO_RSE in ghosts_by_rse:
        g = ghosts_by_rse[NO_RSE]
        rses[NO_RSE] = {
            'type': 'none',
            'ghosts': {'files': g['files'], 'bytes': g['bytes'],
                       'by_state': g['by_state'],
                       'by_campaign': _fold_campaigns(
                           {c: {'files': n} for c, n in g['by_campaign'].items()},
                           campaigns),
                       'oldest_age_s': (round(_seconds_since(g['oldest'], now))
                                        if g.get('oldest') else None)},
            'flow': {key: counters.get(f'rse:{NO_RSE}:{key}', 0)
                     for key in ('ghosts_appeared', 'ghosts_cleared')},
        }

    # Per campaign.
    camp = {}
    marks = ', '.join(['?'] * len(campaigns)) if campaigns else "''"
    tape_marks = ', '.join(['?'] * len(tape)) if tape else "''"
    protection = {c: {'single_copy': 0, 'two_plus': 0, 'disk_only': 0,
                      'tape_only': 0, 'disk_and_tape': 0,
                      'single_copy_old': 0, 'archival_backlog_bytes': 0}
                  for c in campaigns}
    if campaigns:
        for campaign, size, created, avail, tape_avail in db.execute(
                'SELECT f.campaign, f.bytes, f.created_at,'
                " SUM(CASE WHEN r.state = 'AVAILABLE' THEN 1 ELSE 0 END),"
                " SUM(CASE WHEN r.state = 'AVAILABLE' AND r.rse IN"
                f" ({tape_marks}) THEN 1 ELSE 0 END)"
                ' FROM files f LEFT JOIN replicas r ON r.name = f.name'
                f' WHERE f.gone_at IS NULL AND f.campaign IN ({marks})'
                ' GROUP BY f.name', sorted(tape) + list(campaigns)):
            p = protection[campaign]
            avail, tape_avail = int(avail or 0), int(tape_avail or 0)
            if not avail:
                continue
            if avail == 1:
                p['single_copy'] += 1
                age = _seconds_since(created, now)
                if age is not None and age > single_copy_seconds:
                    p['single_copy_old'] += 1
            else:
                p['two_plus'] += 1
            disk_avail = avail - tape_avail
            if disk_avail and tape_avail:
                p['disk_and_tape'] += 1
            elif tape_avail:
                p['tape_only'] += 1
            else:
                p['disk_only'] += 1
                p['archival_backlog_bytes'] += int(size or 0)
    latency_rows = {}
    for campaign, kind, seconds in db.execute(
            'SELECT campaign, kind, seconds FROM latencies WHERE pass_id = ?',
            (pass_info.get('pass_id'),)):
        latency_rows.setdefault(campaign, {}).setdefault(kind, []).append(seconds)
    stalled_rows = []
    for c in campaigns:
        totals = db.execute(
            'SELECT COUNT(*), COALESCE(SUM(bytes), 0),'
            ' SUM(CASE WHEN attached = 0 THEN 1 ELSE 0 END),'
            ' SUM(CASE WHEN events IS NULL THEN 1 ELSE 0 END)'
            ' FROM files WHERE gone_at IS NULL AND campaign = ?', (c,)).fetchone()
        ds = {'total': 0, 'open': 0, 'empty': 0, 'partial_anywhere': 0,
              'quiet_open': 0, 'stalled': 0}
        for name, campaign, root, is_open, summary, rules, length, task_state \
                in dataset_rows:
            if campaign != c:
                continue
            ds['total'] += 1
            rows = json.loads(summary or '[]')
            if not (length or 0):
                ds['empty'] += 1
            elif not any((r.get('available_length') or 0) >= (r.get('length') or 0)
                         and (r.get('length') or 0) > 0 for r in rows):
                ds['partial_anywhere'] += 1
            if is_open:
                ds['open'] += 1
                last = last_registered.get(name.lstrip('/'))
                age = _seconds_since(last, now) if last else None
                if age is not None and age > stalled_seconds:
                    ds['quiet_open'] += 1
                    if task_state == 'active':
                        ds['stalled'] += 1
                        stalled_rows.append((last, name, c))
        p = protection[c]
        camp[c] = {
            'files': int(totals[0] or 0), 'bytes': int(totals[1] or 0),
            'protection': {k: p[k] for k in (
                'single_copy', 'two_plus', 'disk_only', 'tape_only',
                'disk_and_tape', 'single_copy_old')},
            'unattached_files': int(totals[2] or 0),
            'no_events_attr': int(totals[3] or 0),
            'archival_backlog_bytes': p['archival_backlog_bytes'],
            'datasets': ds,
            'flow': {key: counters.get(f'campaign:{c}:{key}', 0) for key in (
                'arrived_files', 'arrived_bytes', 'archived_files',
                'archived_bytes')},
            'latency_s': {kind: _quantiles(values) for kind, values
                          in (latency_rows.get(c) or {}).items()},
        }
        if ds['stalled']:
            verdicts[f'campaign:{c}:datasets_stalled'] = 'warning'
        if p['single_copy_old']:
            verdicts[f'campaign:{c}:single_copy_old'] = 'warning'

    ghost_rows = sorted(
        (row for slot in ghosts_by_rse.values() for row in slot['rows']))
    stuck_rows.sort()
    stalled_rows.sort()
    data = {
        'interval': {'start': _iso(since), 'end': stamp},
        'pass': pass_info,
        'rses': rses,
        'campaigns': camp,
        'exceptions': {
            'ghosts': [[n, r, s, c, b, created] for created, n, r, s, c, b
                       in ghost_rows[:LISTING_HEAD]],
            'stuck_rules': [[d, r, at, n] for at, d, r, n
                            in stuck_rows[:LISTING_HEAD]],
            'stalled_datasets': [[d, c, last] for last, d, c
                                 in stalled_rows[:LISTING_HEAD]],
            'overflow': {
                'ghosts': max(0, len(ghost_rows) - LISTING_HEAD),
                'stuck_rules': max(0, len(stuck_rows) - LISTING_HEAD),
                'stalled_datasets': max(0, len(stalled_rows) - LISTING_HEAD)},
        },
        'thresholds': th,
        'assessment': {
            'verdicts': verdicts,
            'overall': 'warning' if verdicts else 'ok'},
    }
    serialized = len(json.dumps(data, separators=(',', ':')))
    if serialized > MAX_SERIALIZED_BYTES:
        for key in ('ghosts', 'stuck_rules', 'stalled_datasets'):
            rows = data['exceptions'][key]
            data['exceptions']['overflow'][key] += len(rows) - len(rows[:10])
            data['exceptions'][key] = rows[:10]
        serialized = len(json.dumps(data, separators=(',', ':')))
        if serialized > MAX_SERIALIZED_BYTES:
            raise ValueError(
                f'storage projection {serialized} bytes exceeds'
                f' {MAX_SERIALIZED_BYTES}')
    return data


# ---------------------------------------------------------------------------
# The pass
# ---------------------------------------------------------------------------

def run_pass(mode='incremental', campaigns=None, db_path=DEFAULT_DB,
             limit_files=0, limit_datasets=0):
    """Run one pass in the given mode and return (summary, projection).
    A validation run (``limit_files`` or ``limit_datasets``) works on a
    copy of the store so a capped listing never marks files gone in the
    record."""
    import shutil
    import tempfile

    if mode not in ('census', 'full', 'incremental'):
        raise ValueError(f'unknown mode {mode!r}')
    now = dt.datetime.now(dt.timezone.utc)
    if limit_datasets:
        limit_files = limit_files or 1
    if limit_files:
        # The managed scratch root on /data; /tmp is on the small root
        # volume and the store copy can be hundreds of megabytes.
        scratch = tempfile.mkdtemp(
            prefix='storage-pass-',
            dir=os.environ.get('SWF_TMP_DIR') or '/data/swf-tmp')
        copy = os.path.join(scratch, 'storage.sqlite')
        if os.path.exists(db_path):
            shutil.copy(db_path, copy)
        db_path = copy
        log(f'validation run on a copy of the store: {copy}')
    db = open_store(db_path)
    last = db.execute('SELECT finished FROM passes WHERE finished IS NOT NULL'
                      ' ORDER BY id DESC LIMIT 1').fetchone()
    since = _parse_iso(last[0]) if last else None
    if mode == 'incremental' and since is None:
        raise RuntimeError('no completed pass in the store: run a census first')
    if since is None:
        since = now - dt.timedelta(hours=24)
    t0 = time.monotonic()
    catalog = Catalog()
    inventory = dataset_inventory(catalog)
    if campaigns:
        campaigns = tuple(campaigns)
    elif mode == 'census':
        campaigns = tuple(sorted({c for _, c in inventory.values()}))
    else:
        campaigns = target_campaigns()
    cursor = db.execute(
        'INSERT INTO passes (mode, started, campaigns) VALUES (?,?,?)',
        (mode, _iso(now), json.dumps(list(campaigns))))
    pass_id = cursor.lastrowid
    db.commit()
    log(f'pass {pass_id} {mode}: {len(inventory)} datasets under the roots;'
        f' campaigns {", ".join(campaigns)}')

    types = rse_tier(db, catalog, now)
    tape_rses = {rse for rse, t in types.items() if t == 'TAPE'}
    log(f'  RSEs: {", ".join(sorted(types))}; tape: {", ".join(sorted(tape_rses))}')

    names, exhaustive = _search_files(catalog, mode, campaigns, since)
    if mode == 'incremental':
        names |= _standing_names(db, campaigns)
    touched = {'/' + _split(n)[2] for n in names if _split(n)[0]}
    log(f'  {len(names)} files to resolve')

    selected = datasets_to_check(db, mode, inventory, campaigns, touched)
    if limit_datasets:
        # Validation cap: the covered campaigns' datasets first.
        covered = sorted(n for n in selected
                         if inventory.get(n, (None, None))[1] in campaigns)
        selected = set((covered + sorted(selected - set(covered)))
                       [:int(limit_datasets)])
        log(f'capped at {len(selected)} datasets (validation run)')
    content = dataset_tier(db, catalog, now, selected, inventory)
    gone_datasets = [(n,) for (n,) in db.execute(
        'SELECT name FROM datasets WHERE gone_at IS NULL')
        if n not in inventory]
    if gone_datasets and mode != 'incremental':
        db.executemany('UPDATE datasets SET gone_at = ? WHERE name = ?',
                       [(_iso(now), n) for (n,) in gone_datasets])
        db.commit()
    log(f'  {len(content)} datasets refreshed')

    resolved = file_tier(db, catalog, now, since, mode, campaigns, names,
                         exhaustive, content, tape_rses, pass_id,
                         limit_files=limit_files)
    duration = round(time.monotonic() - t0, 1)
    pass_info = {'pass_id': pass_id, 'mode': mode, 'campaigns': list(campaigns),
                 'files_checked': resolved, 'datasets_checked': len(content),
                 'duration_s': duration,
                 'errors': catalog.errors[:10],
                 'error_count': len(catalog.errors)}
    data = projection(db, now, since, campaigns, pass_info)
    finished = dt.datetime.now(dt.timezone.utc)
    db.execute(
        'UPDATE passes SET finished = ?, files_checked = ?,'
        ' datasets_checked = ?, errors = ? WHERE id = ?',
        (_iso(finished), resolved, len(content),
         json.dumps(catalog.errors), pass_id))
    db.execute('DELETE FROM latencies WHERE pass_id < ?', (pass_id - 3,))
    db.commit()
    summary = {'pass_id': pass_id, 'mode': mode, 'campaigns': list(campaigns),
               'files_checked': resolved, 'datasets_checked': len(content),
               'duration_s': duration, 'errors': len(catalog.errors),
               'serialized_bytes': len(json.dumps(data, separators=(',', ':'))),
               'store': db_path}
    log(f'pass {pass_id} done in {duration}s: {resolved} files,'
        f' {len(content)} datasets, {len(catalog.errors)} errors')
    return summary, data
