"""Derive per-file event counts for delivered campaign data
(CAMPAIGN_DELIVERY.md, The events source).

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
count, disk replicas only — never tape) anchors that class's
events-per-file, and every file in the class inherits the anchored
rate. The catalog totals cross-check the assignment per location.

Results land in a SQLite table keyed by DID: measured anchors carry
provenance ``measured``; class members carry ``sampled-rate``;
tape-only classes of dormant, fully delivered sources derive from the
catalog (``catalog-derived``, recomputed every run). The daily record
builder (analytics/delivery_daily.py) joins the store at build time.
Reruns re-attempt unanchored classes and newly arrived files only, so
the nightly pass costs a few file opens at most.

Above them all, ``reported``: the count the job itself wrote on the
file DID at registration, Rucio's ``events`` attribute, which the
epicprod payload sets from its own output since 2026-09-06
(RUCIO_REGISTRATION_CONTRACT.md § 1; a node harness close's merged file
carries it too, NODE_EVENT_DISPATCHER.md). It arrives with the
inventory (the bulk metadata read), costs no file open, and is the
file's own account, so it is taken first, is never replaced by an
inference or a sandbox row, and its files are neither anchors nor
members of a size class.

Runs nightly as a ``catalog_sync`` chain step (the prod-ops agent's
``measure-file-events.py`` doer); runnable by hand through
``scripts/measure_file_events.py``.
"""

import concurrent.futures
import datetime as dt
import json
import os
import sqlite3
import ssl
import threading
import urllib.request

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
DATASETS_CLONE = '/data/wenauseic/github/simulation_campaign_datasets'

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
    from pcs.services import JLAB_RUCIO_URL

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
    from pcs.services import _jlab_rucio_auth

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
    from pcs.services import _jlab_rucio_get

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


def attached_names(get, locations, log=print):
    """{location: the names attached to the location's dataset}, the
    catalog of record's own content, for the locations given.

    A file DID exists before its bytes move: the Rucio upload client
    registers the DID and a COPYING replica, transfers, and only then
    attaches the file to its dataset, and every other writer (the
    payload's registration in place, the registrar, the stash drain)
    attaches in the registering call. A DID a failed upload left behind
    is therefore named under the location but attached to nothing
    (6,707 of them, 261 GB, in one Upsilon location after the BNL-XRD
    door died on 2026-09-20), so attachment, not the name search, is
    what makes a file delivered. ``get(path, **query)`` returns the
    catalog's text. A location that is no dataset in the catalog (404)
    attaches nothing and is reported."""
    import urllib.error

    out = {}
    for location in sorted(locations):
        dataset = '/' + location.lstrip('/')
        try:
            text = get(f'/dids/epic/{dataset}/files', timeout=300)
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
            log(f'WARNING: {location} is no dataset in the catalog; '
                f'its files count as unattached')
            text = ''
        names = set()
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                raise ValueError(f'the file listing of {location} is not '
                                 f'JSON lines: {line[:120]!r}')
            if isinstance(row, dict) and row.get('name'):
                names.add(row['name'])
        out[location] = names
    return out


def delivered_names(get, wanted, log=print):
    """The subset of ``wanted`` (a {name: ...} mapping of file names)
    attached to their datasets, and {location: unattached count} for
    what the name search returned that the catalog holds no delivery
    of."""
    by_location = {}
    for name in wanted:
        by_location.setdefault('/'.join(name.split('/')[:-1]), []).append(name)
    attached = attached_names(get, by_location, log=log)
    kept, unattached = {}, {}
    for location, names in by_location.items():
        present = attached.get(location) or set()
        for name in names:
            if name in present:
                kept[name] = wanted[name]
            else:
                unattached[location] = unattached.get(location, 0) + 1
    return kept, unattached


def collect_inventory(campaigns):
    """{location: [(name, campaign, bytes, created, reported)]} for
    delivered files of the target campaigns; ``reported`` is the
    file's own event count on its DID (Rucio ``events``), or None."""
    from pcs.services import _ndjson, campaign_family

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
    wanted, unattached = delivered_names(rucio_get, wanted, log=log)
    log(f'inventory: {len(names)} files under roots, '
        f'{len(wanted)} delivered in target campaigns, '
        f'{sum(unattached.values())} named but attached to no dataset '
        f'(failed uploads) left out')

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
            reported = row.get('events')
            try:
                reported = int(reported) if reported is not None else None
            except (TypeError, ValueError):
                reported = None
            by_location.setdefault(location, []).append(
                (name, wanted[name], int(row.get('bytes') or 0),
                 row.get('created_at') or '', reported))
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


def measure_file_events(campaigns=None, *, db_path=DEFAULT_DB, workers=6,
                        max_locations=0):
    """Measure/derive events for files not yet in the store. Locations
    are processed newest-activity first; failures are recorded per file
    with the error text and counted in the returned stats. Without
    ``campaigns``, every campaign in the catalog is covered."""
    if not campaigns:
        from .delivery_daily import target_campaigns
        campaigns = target_campaigns()
    db = open_store(db_path)
    # Catalog-derived rows are excluded: they recompute every run so a
    # still-growing source sheds its provisional rate.
    have = {name for (name,) in db.execute(
        "SELECT name FROM file_events WHERE events IS NOT NULL"
        " AND provenance IN ('reported', 'measured', 'sampled-rate', 'sandbox')")}
    # A file's own reported count supersedes an inferred or sandbox row
    # it may already hold; only a reported or measured row is final.
    final = {name for (name,) in db.execute(
        "SELECT name FROM file_events WHERE events IS NOT NULL"
        " AND provenance IN ('reported', 'measured')")}
    log(f'store: {len(have)} files with reported/measured/sampled events, {len(final)} final')

    by_location = collect_inventory(campaigns)

    def created_at(entry):
        # Rucio's created_at is RFC 1123 text ('Mon, 21 Sep 2026 21:43:26
        # UTC'); as text it orders by weekday name, so it is parsed.
        try:
            return dt.datetime.strptime(entry[3], '%a, %d %b %Y %H:%M:%S %Z')
        except (TypeError, ValueError):
            return dt.datetime.min

    # Newest activity first: current production gains coverage first.
    locations = sorted(
        by_location,
        key=lambda loc: max(created_at(e) for e in by_location[loc]),
        reverse=True)
    if max_locations:
        locations = locations[:max_locations]

    db_lock = threading.Lock()
    stats = {'reported': 0, 'anchored': 0, 'filled': 0, 'failed_classes': 0,
             'checked': 0, 'check_off': 0}

    def record(entry, events, provenance, pfn, rse, error):
        name, campaign, size = entry[0], entry[1], entry[2]
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
        # The file's own count first: what the job wrote on the DID.
        # Whatever the store held for it (an inference, a sandbox row)
        # gives way, and the file takes no part in the size classes.
        for entry in by_location[location]:
            if len(entry) > 4 and entry[4] is not None and entry[0] not in final:
                record(entry, entry[4], 'reported', None, None, None)
                stats['reported'] += 1
        entries = [e for e in by_location[location]
                   if e[0] not in have and not (len(e) > 4 and e[4] is not None)]
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

    with concurrent.futures.ThreadPoolExecutor(workers) as pool:
        list(pool.map(process_location, locations))

    log(f'done: {stats["reported"]} files with their own reported count, '
        f'{stats["anchored"]} classes anchored by measurement, '
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
    return stats


# ---------------------------------------------------------------------------
# Exact counts from the PanDA task sandboxes (provenance 'sandbox').
#
# The production team's PanDA path (job_submission_condor's
# submit_panda.py) ships the submission CSV in the task's sandbox, one
# row per job: source file, extension, events per job, chunk index. The
# campaign run script names the outputs from the same row
# (<source basename>.<chunk>.eicrecon.edm4eic.root under the tag built
# from the sandbox environment), so a delivered file maps to its row and
# its row's event count is exact, except a source's last chunk, which
# holds what remains of the source: min(row events, catalog total -
# chunk * row events). No file is read and Rucio is not written; the
# counts land in this store and replace inferred rows (sampled-rate,
# catalog-derived, unmeasured). Measured rows are kept and compared.
# Files with no sandbox row (the condor path) are left as they are.
# ---------------------------------------------------------------------------
SANDBOX_CACHE = '/data/wenauseic/swf-delivery/panda-sandboxes'
SANDBOX_CACHE_URL = 'https://pandaserver01.sdcc.bnl.gov:25443/cache/'
SANDBOX_PROVENANCE = 'sandbox'
SANDBOX_TASK_SQL = (
    'SELECT t.jeditaskid, t.status, t.taskname, p.taskparams'
    ' FROM doma_panda.jedi_tasks t'
    ' JOIN doma_panda.jedi_taskparams p ON p.jeditaskid = t.jeditaskid'
    ' WHERE t.taskname LIKE %s AND p.taskparams LIKE %s'
    ' ORDER BY t.jeditaskid')


def sandbox_tasks(campaign):
    """The campaign's PanDA tasks submitted through the CSV path, from
    the PanDA database (read-only): [{jeditaskid, status, taskname,
    jobo, csvbase}], plus the tasks whose parameters could not be read."""
    import re
    import urllib.parse
    from django.db import connections

    tasks, unread = [], []
    with connections['panda'].cursor() as cur:
        cur.execute(SANDBOX_TASK_SQL,
                    (f'group.EIC.{campaign}%', '%submit_panda.py%'))
        for tid, status, name, params in cur.fetchall():
            jobo = re.search(r'jobO\.[0-9a-f]+\.tar\.gz', params or '')
            base = re.search(r'submit_panda\.py%20\$\{SEQNUMBER\}%20([^"]+)',
                             params or '')
            if not (jobo and base):
                unread.append((tid, name))
                continue
            tasks.append({'jeditaskid': tid, 'status': status,
                          'taskname': name, 'jobo': jobo.group(0),
                          'csvbase': urllib.parse.unquote(base.group(1))})
    return tasks, unread


def fetch_sandbox(jobo):
    """The sandbox's CSV and environment files, cached under
    SANDBOX_CACHE/<jobo stem>/ (the tarball also carries the submitter's
    proxy, which is never written to disk). Returns the directory."""
    import io
    import tarfile

    stem = jobo[:-len('.tar.gz')]
    target = os.path.join(SANDBOX_CACHE, stem)
    if os.path.isdir(target) and any(
            f.endswith('.csv') for f in os.listdir(target)):
        return target
    # The cache answers plain reads from inside SCDF; a proxy, when the
    # environment names one that exists, is presented as the client cert.
    proxy = os.environ.get('EVGEN_X509_PROXY') or os.environ.get('X509_USER_PROXY')
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    if proxy and os.path.exists(proxy):
        ctx.load_cert_chain(proxy)
    with urllib.request.urlopen(SANDBOX_CACHE_URL + jobo, context=ctx,
                                timeout=60) as response:
        blob = response.read()
    os.makedirs(target, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(blob), mode='r:gz') as tar:
        for member in tar.getmembers():
            name = os.path.basename(member.name)
            if not member.isfile():
                continue
            if name.endswith('.csv') or (name.startswith('environment')
                                         and name.endswith('.sh')):
                data = tar.extractfile(member).read()
                with open(os.path.join(target, name), 'wb') as out:
                    out.write(data)
    return target


def sandbox_env(directory):
    """{KEY: value} from the sandbox's environment-*.sh export lines."""
    env = {}
    for filename in os.listdir(directory):
        if not (filename.startswith('environment') and filename.endswith('.sh')):
            continue
        with open(os.path.join(directory, filename)) as f:
            for line in f:
                line = line.strip()
                if line.startswith('export ') and '=' in line:
                    key, value = line[len('export '):].split('=', 1)
                    env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def sandbox_rows(directory, csvbase):
    """[(file_col, ext, events, chunk)] from the sandbox CSV named by
    the task's csvbase (the file the jobs indexed by sequence number)."""
    import csv

    path = os.path.join(directory, csvbase + '.csv')
    if not os.path.exists(path):
        candidates = [f for f in os.listdir(directory) if f.endswith('.csv')]
        if len(candidates) != 1:
            raise FileNotFoundError(f'{csvbase}.csv not in {directory}')
        path = os.path.join(directory, candidates[0])
    rows = []
    with open(path) as f:
        for parsed in csv.reader(f):
            if len(parsed) < 4 or not parsed[2].strip().isdigit():
                continue
            rows.append((parsed[0].strip(), parsed[1].strip(),
                         int(parsed[2]), parsed[3].strip()))
    return rows


def sandbox_reco_name(file_col, chunk, env):
    """The RECO DID the campaign run script writes for a CSV row: the
    tag is <DETECTOR_VERSION>/<DETECTOR_CONFIG>[/<TAG_PREFIX>][/<source
    dir>], the file <TAG_SUFFIX_><source basename>.<chunk>.eicrecon.edm4eic.root."""
    source_dir = os.path.dirname(file_col)
    if source_dir.startswith('EVGEN/'):
        source_dir = source_dir[len('EVGEN/'):]
    parts = [env.get('DETECTOR_VERSION') or 'main',
             env.get('DETECTOR_CONFIG') or '']
    if env.get('TAG_PREFIX'):
        parts.append(env['TAG_PREFIX'].strip('/'))
    if source_dir and source_dir != 'EVGEN':
        parts.append(source_dir.strip('/'))
    tag = '/'.join(p for p in parts if p)
    suffix = env.get('TAG_SUFFIX') or ''
    taskname = f"{suffix + '_' if suffix else ''}{os.path.basename(file_col)}.{chunk}"
    return f'/RECO/{tag}/{taskname}.eicrecon.edm4eic.root'


def sandbox_expected(campaign, max_tasks=0):
    """{RECO DID: {'events', 'source', 'chunk', 'tasks'}} over the
    campaign's sandboxes, with the conflicts (one DID, two event
    counts) and per-task notes. Reads the PanDA database and the
    sandbox cache only."""
    tasks, unread = sandbox_tasks(campaign)
    if max_tasks:
        tasks = tasks[:max_tasks]
    expected, conflicts, notes = {}, {}, []
    for task in tasks:
        try:
            directory = fetch_sandbox(task['jobo'])
            env = sandbox_env(directory)
            rows = sandbox_rows(directory, task['csvbase'])
        except Exception as exc:                            # noqa: BLE001
            notes.append(f"task {task['jeditaskid']} {task['taskname']}: {exc}")
            continue
        if str(env.get('COPYRECO', 'true')).lower() != 'true':
            notes.append(f"task {task['jeditaskid']}: COPYRECO is not true; skipped")
            continue
        task['rows'] = len(rows)
        for file_col, _ext, events, chunk in rows:
            did = sandbox_reco_name(file_col, chunk, env)
            entry = expected.get(did)
            if entry is None:
                expected[did] = {'events': events,
                                 'source': os.path.basename(file_col),
                                 'chunk': int(chunk) if chunk.isdigit() else None,
                                 'tasks': [task['jeditaskid']]}
            elif entry['events'] != events:
                conflicts.setdefault(did, set()).update(
                    {entry['events'], events})
                entry['tasks'].append(task['jeditaskid'])
            else:
                entry['tasks'].append(task['jeditaskid'])
    for tid, name in unread:
        notes.append(f'task {tid} {name}: parameters carry no sandbox or csv name')
    return {'tasks': tasks, 'expected': expected, 'conflicts': conflicts,
            'notes': notes}


def apply_sandbox_counts(campaign, *, db_path=DEFAULT_DB, apply=False,
                         max_tasks=0):
    """Reconcile the sandbox counts with the store for one campaign and,
    with ``apply``, write them (provenance 'sandbox') over inferred rows.
    Returns the statistics; every skipped or disagreeing file is listed
    in the returned samples, never dropped silently."""
    import statistics

    found = sandbox_expected(campaign, max_tasks=max_tasks)
    expected, conflicts = found['expected'], found['conflicts']
    log(f"sandboxes: {len(found['tasks'])} tasks, "
        f"{sum(t.get('rows', 0) for t in found['tasks'])} rows, "
        f"{len(expected)} expected RECO files, {len(conflicts)} conflicts, "
        f"{len(found['notes'])} notes")
    for note in found['notes']:
        log(f'  note: {note}')

    db = open_store(db_path)
    store = {}
    for name, size, events, provenance, location in db.execute(
            'SELECT name, bytes, events, provenance, location FROM file_events'
            ' WHERE campaign = ?', (campaign,)):
        store[name] = (size, events, provenance, location)
    log(f'store: {len(store)} files of campaign {campaign}')

    stats = {'expected': len(expected), 'conflicts': len(conflicts),
             'not_in_store': 0, 'matched': 0, 'last_chunks': 0,
             'last_chunk_catalog': 0, 'last_chunk_bytes_ok': 0,
             'last_chunk_uncertain': 0, 'anomalies': 0,
             'measured_agree': 0, 'measured_differ': 0,
             'inferred_agree': 0, 'inferred_differ': 0,
             'unmeasured_filled': 0, 'written': 0,
             'unguarded': 0, 'undersized': 0,
             'events_before': 0, 'events_after': 0}
    samples = {'measured_differ': [], 'inferred_differ': [],
               'uncertain': [], 'anomalies': [], 'conflicts': [],
               'unguarded': [], 'undersized': []}
    for did, values in list(conflicts.items())[:20]:
        samples['conflicts'].append((did, sorted(values)))

    # Group the matched files by (location, source) for the last-chunk rule.
    by_source = {}
    for did, entry in expected.items():
        if did in conflicts:
            continue
        row = store.get(did)
        if row is None:
            stats['not_in_store'] += 1
            continue
        stats['matched'] += 1
        key = (row[3], entry['source'])
        by_source.setdefault(key, []).append((did, entry, row))

    catalog_cache = {}

    def catalog(location):
        if location not in catalog_cache:
            catalog_cache[location] = catalog_rows(location) or {}
        return catalog_cache[location]

    # The size guard: chunks of one location planned at the same event
    # count are the same size up to compression jitter, across the
    # location's source files; a file well under that median holds fewer
    # events than its row planned (an input shorter than assumed, an
    # event dropped) and gets no planned count. Fewer than three sized
    # siblings is no basis to judge.
    sizes_by_class = {}
    for (location, _source), members in by_source.items():
        for _did, entry, row in members:
            if row[0]:
                sizes_by_class.setdefault((location, entry['events']), []).append(row[0])
    medians = {key: statistics.median(sizes)
               for key, sizes in sizes_by_class.items() if len(sizes) >= 3}

    decided = {}   # did -> exact events
    for (location, source), members in by_source.items():
        chunks = [m for m in members if m[1]['chunk'] is not None]
        last = max(chunks, key=lambda m: m[1]['chunk']) if chunks else None
        for did, entry, row in members:
            events = entry['events']
            median = medians.get((location, events))
            sizes = sizes_by_class.get((location, events), [])
            if median is None:
                stats['unguarded'] += 1
                if len(samples['unguarded']) < 20:
                    samples['unguarded'].append((did, f'{len(sizes)} sized siblings'))
                continue
            if last is None or did != last[0]:
                if not row[0] or row[0] < (1 - SIZE_TOLERANCE) * median:
                    stats['undersized'] += 1
                    if len(samples['undersized']) < 20:
                        samples['undersized'].append(
                            (did, row[0], int(median), row[2], row[1]))
                    continue
            if last is not None and did == last[0]:
                stats['last_chunks'] += 1
                total = catalog(location).get(source)
                if total:
                    remain = total - entry['chunk'] * events
                    if remain <= 0:
                        stats['anomalies'] += 1
                        if len(samples['anomalies']) < 20:
                            samples['anomalies'].append(
                                (did, f'catalog total {total} leaves no events'
                                      f' for chunk {entry["chunk"]}'))
                        continue
                    events = min(events, remain)
                    stats['last_chunk_catalog'] += 1
                else:
                    if row[0] and row[0] >= (1 - SIZE_TOLERANCE) * median:
                        stats['last_chunk_bytes_ok'] += 1
                    else:
                        stats['last_chunk_uncertain'] += 1
                        if len(samples['uncertain']) < 20:
                            samples['uncertain'].append(
                                (did, 'last chunk, no catalog total, size not'
                                      ' in its class'))
                        continue
            decided[did] = events

    now = dt.datetime.utcnow().isoformat()
    for did, events in decided.items():
        size, old_events, provenance, location = store[did]
        if old_events is not None:
            stats['events_before'] += old_events
        stats['events_after'] += events
        if provenance in ('measured', 'reported'):
            if old_events == events:
                stats['measured_agree'] += 1
            else:
                stats['measured_differ'] += 1
                if len(samples['measured_differ']) < 20:
                    samples['measured_differ'].append((did, old_events, events))
            continue        # a measured or reported row is truth; never replaced
        if old_events is None:
            stats['unmeasured_filled'] += 1
        elif old_events == events:
            stats['inferred_agree'] += 1
        else:
            stats['inferred_differ'] += 1
            if len(samples['inferred_differ']) < 20:
                samples['inferred_differ'].append(
                    (did, provenance, old_events, events))
        if apply:
            db.execute(
                'INSERT OR REPLACE INTO file_events'
                ' (name, campaign, location, bytes, events, provenance,'
                '  pfn, rse, error, measured_at)'
                ' VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?)',
                (did, campaign, location, size, events, SANDBOX_PROVENANCE, now))
            stats['written'] += 1
    if apply:
        db.commit()
    db.close()
    log(f"{'applied' if apply else 'dry run'}: {stats}")
    for kind, rows in samples.items():
        for row in rows:
            log(f'  {kind}: {row}')
    return stats, samples
