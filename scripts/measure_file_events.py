"""Derive per-file event counts for delivered campaign data.

Events per output file is not directly recorded: production configs
cover only PCS-submitted tasks, Rucio's native ``events`` field is
unpopulated at registration, and the condor chunker computed each
submission's chunk size from that day's timing, discarding the chunk
lists. What IS recorded: Rucio carries every delivered file's size, the
ANL campaign catalog (eicweb project 491 CI artifacts, the feed the
condor submitter reads) carries exact event totals per EVGEN source
file, and chunks of one source are equal-sized. So within one dataset
location the files cluster into a few uniform byte-size classes, one
per chunking; one xrootd read per class (uproot, events tree entry
count) anchors that class's events-per-file, and every file in the
class inherits the anchored rate. The catalog totals cross-check the
assignment per location. This costs a few hundred file opens per
campaign, once, instead of one per file.

Results land in a SQLite table keyed by DID: measured anchors carry
provenance ``measured``; class members carry ``sampled-rate``. The
daily delivery record builder (``backfill_delivery_history.py``) reads
the store to report events alongside files.

Run under the venv with the swf-monitor project on the path:

    cd <swf-monitor>/src && source <venv>/bin/activate && source ~/.env
    python <swf-epicprod>/scripts/measure_file_events.py \\
        [--campaigns 26.06,26.07] [--locations N] [--workers 6]

The store defaults to /data/wenauseic/swf-delivery/file_events.sqlite.
Locations are processed newest-activity first. Failures are recorded
per file with the error text and reported in the summary; reruns
re-attempt unanchored classes and newly arrived files only.
"""

import argparse
import concurrent.futures
import datetime as dt
import json
import os
import sqlite3
import ssl
import sys
import threading
import urllib.request

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402

django.setup()

from pcs.services import (JLAB_RUCIO_URL, _jlab_rucio_auth,  # noqa: E402
                          _jlab_rucio_get, _ndjson, campaign_family)

ROOTS = ('/RECO', '/SIMU')
SEARCH_EPOCH = '2026-01-01T00:00:00'
BULK_CHUNK = 500
DEFAULT_DB = '/data/wenauseic/swf-delivery/file_events.sqlite'
XROOTD_TIMEOUT = 120
# Files whose sizes agree within this fraction belong to one chunk
# class (chunks of one source differ only in compression jitter).
SIZE_TOLERANCE = 0.05
CATALOG_BASE = ('https://eicweb.phy.anl.gov/api/v4/projects/491/jobs/'
                'artifacts/main/raw/results/nightly/epic_craterlake/'
                'main/datasets/timings/')
CATALOG_JOB = '?job=collect'

_print_lock = threading.Lock()


def log(message):
    with _print_lock:
        print(f'[{dt.datetime.now():%H:%M:%S}] {message}', flush=True)


def open_store(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # All cross-thread access is serialized by the caller's lock.
    db = sqlite3.connect(path, check_same_thread=False)
    db.execute(
        'CREATE TABLE IF NOT EXISTS file_events ('
        ' name TEXT PRIMARY KEY, campaign TEXT, location TEXT,'
        ' bytes INTEGER, events INTEGER, provenance TEXT, pfn TEXT,'
        ' rse TEXT, error TEXT, measured_at TEXT)')
    db.commit()
    return db


def _rucio_post(token, path, body):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(JLAB_RUCIO_URL + path,
                                 data=json.dumps(body).encode(),
                                 method='POST')
    req.add_header('X-Rucio-Auth-Token', token)
    req.add_header('Content-Type', 'application/json')
    text = urllib.request.urlopen(req, context=ctx, timeout=120).read()
    return [json.loads(line) for line in text.decode().splitlines()
            if line.strip()]


# The Rucio token outlives no long run: every call goes through a
# holder that re-authenticates once on 401 and retries.
_token_lock = threading.Lock()
_token_cache = {'value': None}


def _current_token():
    with _token_lock:
        if _token_cache['value'] is None:
            _token_cache['value'] = _jlab_rucio_auth()
        return _token_cache['value']


def _drop_token(stale):
    with _token_lock:
        if _token_cache['value'] == stale:
            _token_cache['value'] = None


def rucio_post(path, body):
    for attempt in (0, 1):
        token = _current_token()
        try:
            return _rucio_post(token, path, body)
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and attempt == 0:
                _drop_token(token)
                continue
            raise
    return []


def rucio_get(path, **params):
    for attempt in (0, 1):
        token = _current_token()
        try:
            return _jlab_rucio_get(path, token, **params)
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and attempt == 0:
                _drop_token(token)
                continue
            raise
    return ''


def collect_inventory(campaigns):
    """{location: [(name, campaign, bytes, created)]} for delivered
    files of the target campaigns."""
    names = []
    for root in ROOTS:
        found = _ndjson(rucio_get(
            '/dids/epic/dids/search',
            type='file', created_after=SEARCH_EPOCH, name=root + '/*'))
        names.extend(n for n in found if isinstance(n, str))
    wanted = {}
    for name in names:
        segs = name.split('/')
        if len(segs) < 4 or not segs[2]:
            continue
        family = campaign_family(segs[2])
        if family in campaigns:
            wanted[name] = family
    log(f'inventory: {len(names)} files under roots, '
        f'{len(wanted)} in target campaigns')

    by_location = {}
    ordered = sorted(wanted)
    for start in range(0, len(ordered), BULK_CHUNK):
        chunk = ordered[start:start + BULK_CHUNK]
        for row in rucio_post('/dids/bulkmeta',
                              {'dids': [{'scope': 'epic', 'name': n}
                                        for n in chunk]}):
            name = row.get('name')
            if name not in wanted:
                continue
            location = '/'.join(name.split('/')[:-1])
            by_location.setdefault(location, []).append(
                (name, wanted[name], int(row.get('bytes') or 0),
                 row.get('created_at') or ''))
    return by_location


def size_classes(entries):
    """Group (name, campaign, bytes, created) entries into byte-size
    classes; each class is one chunking's uniform output size."""
    classes = []
    for entry in sorted(entries, key=lambda e: e[2]):
        placed = False
        for cls in classes:
            if abs(entry[2] - cls['ref']) <= SIZE_TOLERANCE * cls['ref']:
                cls['members'].append(entry)
                placed = True
                break
        if not placed:
            classes.append({'ref': entry[2], 'members': [entry]})
    return classes


def disk_pfn(name):
    rows = rucio_post('/replicas/list',
                      {'dids': [{'scope': 'epic', 'name': name}]})
    best = None
    for row in rows:
        for pfn, info in (row.get('pfns') or {}).items():
            if info.get('type') != 'DISK':
                continue
            rank = (0 if info.get('rse') == 'BNL-XRD' else 1,
                    info.get('priority') or 99)
            if best is None or rank < best[2]:
                best = (pfn, info.get('rse'), rank)
    return (best[0], best[1]) if best else (None, None)


def count_events(pfn):
    import uproot
    with uproot.open(pfn, timeout=XROOTD_TIMEOUT) as f:
        keys = {k.split(';')[0] for k in f.keys(recursive=False)}
        if 'events' in keys:
            return int(f['events'].num_entries)
        # A podio file with metadata trees but no events tree is a
        # genuinely eventless output: zero events, not an error.
        if 'podio_metadata' in keys or 'metadata' in keys:
            return 0
        raise ValueError(f'no events tree; keys {sorted(keys)}')


DATASETS_CLONE = '/data/wenauseic/github/simulation_campaign_datasets'
_catalog_index = None
_catalog_lock = threading.Lock()


def _catalog_paths():
    """{location suffix: catalog csv relative path}, indexed from the
    local simulation_campaign_datasets clone: each manifest row's first
    field is a source-file path whose directory IS the delivered
    location's suffix."""
    index = {}
    for dirpath, _dirs, files in os.walk(DATASETS_CLONE):
        if '.git' in dirpath:
            continue
        for filename in files:
            if not filename.endswith('.csv'):
                continue
            path = os.path.join(dirpath, filename)
            rel = os.path.relpath(path, DATASETS_CLONE)
            try:
                with open(path) as f:
                    # Every row: a manifest lists many locations (the
                    # single-particle CSVs carry one row per energy).
                    for line in f:
                        first = line.split(',')[0]
                        if '/' in first:
                            index[os.path.dirname(first)] = rel
            except OSError:
                continue
    return index


def catalog_rows(location):
    """{source basename: recorded event total} for a location, or None
    when the location has no catalog listing (e.g. PanDA-path datasets
    outside the nightly collection)."""
    global _catalog_index
    with _catalog_lock:
        if _catalog_index is None:
            _catalog_index = _catalog_paths()
    # location is '/ROOT/version/detector_config/<catalog suffix>';
    # the leading slash makes split()[0] empty, so the suffix starts
    # at index 4.
    suffix = '/'.join(location.split('/')[4:])
    rel = _catalog_index.get(suffix)
    if not rel:
        return None
    url = CATALOG_BASE + urllib.request.quote(rel) + CATALOG_JOB
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            text = response.read().decode()
    except Exception:                                       # noqa: BLE001
        return None
    rows = {}
    for line in text.splitlines():
        parts = line.split(',')
        # Only this location's rows: one manifest can list many
        # locations (per-energy rows in the single-particle CSVs).
        if (len(parts) >= 3 and parts[2].isdigit()
                and os.path.dirname(parts[0]) == suffix):
            rows[os.path.basename(parts[0])] = int(parts[2])
    return rows or None


def catalog_total(location):
    rows = catalog_rows(location)
    return sum(rows.values()) if rows else None


CHUNK_RE = None


def source_of(name):
    """(source basename, chunk index) parsed from a delivered file
    name — '<source>.<NNNN>.<stage suffixes>' — or (None, None)."""
    import re
    global CHUNK_RE
    if CHUNK_RE is None:
        CHUNK_RE = re.compile(r'^(?P<src>.+)\.(?P<chunk>\d{4})\.')
    match = CHUNK_RE.match(os.path.basename(name))
    if not match:
        return None, None
    return match.group('src'), int(match.group('chunk'))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--campaigns', default='26.06,26.07')
    parser.add_argument('--db', default=DEFAULT_DB)
    parser.add_argument('--workers', type=int, default=6)
    parser.add_argument('--locations', type=int, default=0,
                        help='process at most N locations (0 = all)')
    args = parser.parse_args()
    campaigns = [c.strip() for c in args.campaigns.split(',') if c.strip()]

    db = open_store(args.db)
    # Catalog-derived rows are excluded: they recompute every run so a
    # still-growing source sheds its provisional rate.
    have = {name for (name,) in db.execute(
        "SELECT name FROM file_events WHERE events IS NOT NULL"
        " AND provenance IN ('measured', 'sampled-rate')")}
    log(f'store: {len(have)} files with measured/sampled events')

    by_location = collect_inventory(campaigns)
    # Newest activity first: current production gains coverage first.
    locations = sorted(
        by_location,
        key=lambda loc: max(e[3] for e in by_location[loc]),
        reverse=True)
    if args.locations:
        locations = locations[:args.locations]

    db_lock = threading.Lock()
    stats = {'anchored': 0, 'filled': 0, 'failed_classes': 0,
             'checked': 0, 'check_off': 0}

    def record(entry, events, provenance, pfn, rse, error):
        name, campaign, size, _created = entry
        with db_lock:
            db.execute(
                'INSERT OR REPLACE INTO file_events'
                ' (name, campaign, location, bytes, events, provenance,'
                '  pfn, rse, error, measured_at)'
                ' VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (name, campaign, '/'.join(name.split('/')[:-1]), size,
                 events, provenance, pfn, rse, error,
                 dt.datetime.utcnow().isoformat()))
            db.commit()

    def process_location(location):
        entries = [e for e in by_location[location] if e[0] not in have]
        if not entries:
            return
        for cls in size_classes(entries):
            members = cls['members']
            # Anchor on the median-size member: never the smallest (a
            # last-chunk remainder) nor an outlier. A failed anchor —
            # timeout, missing replica, a damaged file — falls back to
            # further candidates before the class is declared failed.
            ranked = sorted(members, key=lambda e: e[2])
            middle = len(ranked) // 2
            picked = set()
            candidates = []
            for i in (middle, middle + 1, middle - 1, len(ranked) - 1):
                if 0 <= i < len(ranked) and i not in picked:
                    picked.add(i)
                    candidates.append(ranked[i])
            anchor = events = pfn = rse = None
            errors = []
            zeroed = set()
            for candidate in candidates[:3]:
                pfn, rse = disk_pfn(candidate[0])
                if pfn is None:
                    errors.append(f'{candidate[0]}: no disk replica')
                    continue
                try:
                    events = count_events(pfn)
                except Exception as exc:                    # noqa: BLE001
                    errors.append(f'{candidate[0]}: {exc}')
                    continue
                if events == 0 and len(members) > 1:
                    # An eventless file is measured truth for itself
                    # but an anomaly, not a class representative.
                    record(candidate, 0, 'measured', pfn, rse, None)
                    zeroed.add(candidate[0])
                    errors.append(f'{candidate[0]}: eventless')
                    continue
                anchor = candidate
                break
            if anchor is None:
                # No readable replica (tape-only class): derive from
                # recorded metadata alone. For each catalog-listed
                # source, events/file = recorded source total divided
                # by delivered chunk count, valid when the source's
                # chunks are fully delivered (contiguous from 0).
                # Derived rows are recomputed every run, so a source
                # still growing never keeps a stale rate.
                derived = 0
                # Dormancy guard: a contiguous chunk range is also what
                # a partially delivered in-order source looks like, so
                # derivation applies only where arrivals have stopped —
                # an active location must wait for a readable replica
                # or its completion.
                def _created(entry):
                    try:
                        return dt.datetime.strptime(
                            entry[3], '%a, %d %b %Y %H:%M:%S %Z')
                    except (ValueError, TypeError):
                        return dt.datetime.min
                newest = max(_created(e) for e in by_location[location])
                dormant = (dt.datetime.utcnow() - newest
                           > dt.timedelta(days=3))
                rows = catalog_rows(location) if dormant else None
                if rows:
                    by_source = {}
                    for entry in members:
                        src, chunk = source_of(entry[0])
                        if src is not None:
                            by_source.setdefault(src, []).append(
                                (entry, chunk))
                    for src, chunk_entries in by_source.items():
                        total = rows.get(src)
                        chunks = sorted(c for _e, c in chunk_entries)
                        complete = (total and chunks[0] == 0
                                    and chunks[-1] == len(chunks) - 1)
                        if not complete:
                            continue
                        rate = total // len(chunks)
                        for entry, _chunk in chunk_entries:
                            record(entry, rate, 'catalog-derived',
                                   None, None, None)
                            derived += 1
                    if derived:
                        stats['filled'] += derived
                        log(f'{location}: {derived} tape-only files '
                            f'catalog-derived')
                for entry in members:
                    if entry[0] not in zeroed:
                        with db_lock:
                            known = db.execute(
                                'SELECT events FROM file_events'
                                ' WHERE name = ?',
                                (entry[0],)).fetchone()
                        if known and known[0] is not None:
                            continue
                        record(entry, None, None, None, None,
                               '; '.join(errors)[:500])
                if derived < len(members):
                    stats['failed_classes'] += 1
                    log(f'CLASS-FAIL {location} '
                        f'({len(members) - derived} of {len(members)} '
                        f'files): ' + '; '.join(errors))
                continue
            record(anchor, events, 'measured', pfn, rse, None)
            stats['anchored'] += 1
            for entry in members:
                if entry[0] != anchor[0] and entry[0] not in zeroed:
                    record(entry, events, 'sampled-rate', None, None,
                           None)
                    stats['filled'] += 1
        # Catalog cross-check: assigned events vs recorded source
        # totals, reported when the location is catalog-listed.
        total = catalog_total(location)
        if total is not None:
            with db_lock:
                assigned = db.execute(
                    'SELECT sum(events) FROM file_events'
                    ' WHERE location = ? AND events IS NOT NULL',
                    (location,)).fetchone()[0] or 0
            stats['checked'] += 1
            fraction = assigned / total if total else 0
            if fraction > 1.02:
                stats['check_off'] += 1
                log(f'CHECK {location}: assigned {assigned} exceeds '
                    f'catalog total {total}')
            else:
                log(f'{location}: {assigned}/{total} events '
                    f'({fraction:.0%} of catalog total)')

    with concurrent.futures.ThreadPoolExecutor(args.workers) as pool:
        list(pool.map(process_location, locations))

    log(f'done: {stats["anchored"]} classes anchored by measurement, '
        f'{stats["filled"]} files filled at the anchored rate, '
        f'{stats["failed_classes"]} classes failed, '
        f'{stats["checked"]} catalog checks '
        f'({stats["check_off"]} over total)')
    for campaign in campaigns:
        row = db.execute(
            'SELECT count(*), sum(events) FROM file_events'
            ' WHERE campaign = ? AND events IS NOT NULL',
            (campaign,)).fetchone()
        log(f'{campaign}: {row[0]} files carrying events, '
            f'{row[1] or 0} events')
    return 0


if __name__ == '__main__':
    sys.exit(main())
