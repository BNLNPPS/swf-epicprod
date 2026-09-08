"""Attempt manifests: the work-unit record of every PanDA attempt.

A production attempt runs one job per manifest row, ``file,ext,nevents,
ichunk``; the payload skips ``ichunk × nevents`` events into the input
and produces one output named by the row (JEDI_INTEGRATION.md §
Payload-managed output naming). The residual of an attempt, the rows
whose output was never delivered, therefore needs the rows that attempt
actually ran, at their own ``nevents`` and ``ichunk``. This module
records them and recovers them, in the order of the evidence:

- **the record on the attempt** (``PandaTasks.metadata['manifest']``),
  written at submission for PCS attempts and harvested nightly for the
  rest by the sandbox keepalive;
- **the attempt's sandbox** in the PanDA cache, which carries the exact
  CSV the jobs read, for as long as the keepalive holds it;
- **reconstruction** from the definition's per-file event totals and
  the attempt's row count: under the legacy chunking rule every file is
  cut into equal chunks at one per-task size, each file's chunk count
  falls as that size rises, and their sum is the row count, so the row
  count fixes every file's chunk count uniquely and the rows follow.
  It is verified against the delivered outputs and never assumed.

The record is compact and lossless: one entry per input file when its
rows are contiguous chunks at one ``nevents``, an explicit chunk list
otherwise, about a kilobyte per attempt against hundreds of kilobytes
of rows. Whenever both the exact manifest and the reconstruction are in
hand they are compared, and a disagreement is reported as a fact about
the attempt, never resolved silently.
"""
import hashlib
import io
import json
import re
import tarfile
import time
from datetime import datetime, timezone

MANIFEST_ROW_RE = re.compile(
    r'^(?P<file>[^,\s]+),(?P<ext>[^,\s]+),(?P<nevents>\d+),(?P<chunk>\d{4})$')
TARBALL_RE = re.compile(r'jobO\.[0-9a-f]{32}\.tar\.gz')
SOURCE_URL_RE = re.compile(r'"sourceURL":\s*"([^"]+)"')
N_EVENTS_RE = re.compile(r'"nEvents":\s*(\d+)')
CACHE_FETCH_TIMEOUT = 60


class ManifestUnavailable(Exception):
    """The attempt's manifest could not be established; the message names
    the evidence that was missing."""


# ---------------------------------------------------------------- rows

def parse_rows(text):
    """The manifest rows of a CSV text as (file, ext, nevents, chunk)."""
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = MANIFEST_ROW_RE.match(line)
        if m:
            rows.append((m['file'], m['ext'], int(m['nevents']),
                         int(m['chunk'])))
    return rows


def format_rows(rows):
    """Rows back to the manifest's own line form."""
    return [f'{f},{e},{n},{c:04d}' for f, e, n, c in rows]


def rows_sha256(rows):
    canon = '\n'.join(sorted(format_rows(rows)))
    return hashlib.sha256(canon.encode()).hexdigest()


def compact(rows):
    """The compact record of ``rows``: per file, ``nevents`` and the chunk
    count when the chunks are 0..n-1 at one size, else the explicit
    ``[nevents, chunk]`` list."""
    by_file = {}
    for f, e, n, c in rows:
        by_file.setdefault((f, e), []).append((c, n))
    files = []
    for (f, e), chunks in by_file.items():
        chunks.sort()
        sizes = {n for _c, n in chunks}
        indices = [c for c, _n in chunks]
        if len(sizes) == 1 and indices == list(range(len(indices))):
            files.append({'file': f, 'ext': e, 'nevents': sizes.pop(),
                          'nchunks': len(indices)})
        else:
            files.append({'file': f, 'ext': e,
                          'chunks': [[n, c] for c, n in chunks]})
    return {'rows': len(rows), 'files': files, 'sha256': rows_sha256(rows)}


def expand(record):
    """The rows of a compact record, as (file, ext, nevents, chunk)."""
    rows = []
    for entry in (record or {}).get('files') or []:
        f, e = entry['file'], entry['ext']
        if 'chunks' in entry:
            rows.extend((f, e, int(n), int(c)) for n, c in entry['chunks'])
        else:
            n = int(entry['nevents'])
            rows.extend((f, e, n, c) for c in range(int(entry['nchunks'])))
    return rows


# -------------------------------------------------------------- record

def record_of(panda_tasks):
    return (panda_tasks.metadata or {}).get('manifest') or None


def make_record(rows, source):
    """The compact record of ``rows`` with its provenance."""
    record = compact(rows)
    record['source'] = source
    record['recorded_at'] = datetime.now(timezone.utc).isoformat(
        timespec='seconds')
    return record


def store(panda_tasks, rows, source):
    """Write the compact record of ``rows`` on the attempt with its
    provenance. Returns the record."""
    record = make_record(rows, source)
    meta = dict(panda_tasks.metadata or {})
    meta['manifest'] = record
    panda_tasks.metadata = meta
    panda_tasks.save(update_fields=['metadata', 'updated_at'])
    return record


# ------------------------------------------------------- PanDA cache (A)

def _taskparams(jedi_task_id):
    from django.db import connections
    with connections['panda'].cursor() as cur:
        cur.execute('SELECT taskparams FROM jedi_taskparams WHERE jeditaskid = %s',
                    [int(jedi_task_id)])
        row = cur.fetchone()
    if not row or not row[0]:
        return ''
    return row[0] if isinstance(row[0], str) else row[0].read()


def sandbox_ref(jedi_task_id):
    """(source_url, tarball) of the attempt's sandbox from its stored task
    parameters, or (None, None)."""
    text = _taskparams(jedi_task_id)
    tarballs = sorted(set(TARBALL_RE.findall(text)))
    sources = sorted(set(SOURCE_URL_RE.findall(text)))
    if not tarballs or not sources:
        return None, None
    return sources[0], tarballs[0]


def attempt_row_count(jedi_task_id):
    """The number of jobs the attempt was submitted with: ``nEvents`` in
    its task parameters, which the legacy submitter and ours both set to
    the manifest's row count."""
    m = N_EVENTS_RE.search(_taskparams(jedi_task_id))
    return int(m.group(1)) if m else None


def fetch_from_cache(jedi_task_id):
    """The manifest rows read from the attempt's sandbox in the PanDA
    cache: one GET, no credential (pilots fetch sandboxes the same way).
    Raises ManifestUnavailable when the sandbox is gone or holds no
    manifest."""
    import requests
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    source_url, tarball = sandbox_ref(jedi_task_id)
    if not tarball:
        raise ManifestUnavailable(
            f'PanDA task {jedi_task_id} records no sandbox tarball')
    url = f'{source_url}/cache/{tarball}'
    try:
        r = requests.get(url, timeout=CACHE_FETCH_TIMEOUT, verify=False)
    except requests.RequestException as e:
        raise ManifestUnavailable(f'sandbox fetch failed: {e}')
    if r.status_code == 404:
        raise ManifestUnavailable(
            f'sandbox {tarball} is no longer in the PanDA cache')
    if r.status_code != 200:
        raise ManifestUnavailable(
            f'sandbox fetch returned HTTP {r.status_code}')
    best = []
    try:
        with tarfile.open(fileobj=io.BytesIO(r.content), mode='r:gz') as tar:
            for member in tar.getmembers():
                if not member.isfile() or not member.name.endswith('.csv'):
                    continue
                rows = parse_rows(
                    tar.extractfile(member).read().decode('utf-8', 'replace'))
                if len(rows) > len(best):
                    best = rows
    except (tarfile.TarError, OSError) as e:
        raise ManifestUnavailable(f'sandbox {tarball} unreadable: {e}')
    if not best:
        raise ManifestUnavailable(
            f'sandbox {tarball} holds no manifest rows')
    return best


# ------------------------------------------------------ reconstruction (B)

def _inventory_definitions(tails):
    """The definitions inventory entries whose EVGEN tail is one of
    ``tails`` (lowercased directory below EVGEN), from the sweep's
    snapshot."""
    import os
    from .definitions_sweep import SNAPSHOT_NAME
    from .services import RUCIO_SNAPSHOT_DIR
    path = os.path.join(RUCIO_SNAPSHOT_DIR, SNAPSHOT_NAME)
    try:
        with open(path) as f:
            definitions = json.load(f).get('definitions') or []
    except (OSError, ValueError):
        return []
    return [d for d in definitions if str(d.get('tail') or '') in tails]


RECO_DIR_RE = re.compile(r'^/?RECO/[^/]+/[^/]+/(?:try\d+/)?(?P<tail>.+?)/?$')


def input_dirs(task, rows=None):
    """The input directories below EVGEN an attempt ran over, lowercased:
    from its manifest rows when in hand, else from the task's own
    recorded RECO outputs (``RECO/<ver>/<config>[/tryN]/<input dir>``),
    which is what the attempt produced and needs no other resolution."""
    if rows:
        return {str(f).rpartition('/')[0].lower().strip('/')
                for f, _e, _n, _c in rows}
    tails = set()
    for entry in ((task.overrides or {}).get('outputs') or []):
        if str(entry.get('stage', '')).upper() != 'RECO':
            continue
        name = str(entry.get('did') or '').partition(':')[2] or str(
            entry.get('did') or '')
        m = RECO_DIR_RE.match(name)
        if m:
            tails.add(m['tail'].lower().strip('/'))
    return tails


def definition_files(task, tails):
    """[(file, ext, ntotal)] of the definitions behind the input
    directories ``tails``: the artifact rows the definitions sweep keeps
    as the cost's ``files``. The task's dataset record is read first,
    then the sweep's inventory. A cost record from before the field
    existed is completed by one fetch of the definition's timings
    artifact, on this demand only, and kept on the task's dataset for
    every later residual."""
    tails = {str(t).lower().strip('/') for t in (tails or ())}
    ds = getattr(task, 'dataset', None)
    meta = (ds.metadata or {}) if ds is not None else {}
    defs = meta.setdefault('definitions', {})
    matched = defs.setdefault('matched', [])
    candidates = [m for m in matched if str(m.get('tail') or '') in tails]
    changed = False
    if not candidates and tails:
        for d in _inventory_definitions(tails):
            entry = {'path': d.get('path'), 'tail': d.get('tail'),
                     'registered': d.get('registered'),
                     'cost': d.get('cost')}
            matched.append(entry)
            candidates.append(entry)
            changed = True
    out = []
    for entry in candidates:
        cost = entry.get('cost') or {}
        if not cost.get('files') and entry.get('path'):
            from .definitions_sweep import _fetch_cost
            fetched = _fetch_cost(entry['path'], [])
            if isinstance(fetched, dict) and fetched.get('files'):
                cost = {**cost, 'files': fetched['files']}
                entry['cost'] = cost
                changed = True
        for row in (cost.get('files') or []):
            try:
                out.append((str(row[0]), str(row[1]), int(row[2])))
            except (IndexError, TypeError, ValueError):
                continue
    if changed and ds is not None:
        ds.metadata = meta
        ds.save(update_fields=['metadata'])
    return out


def _stem_key(file_col):
    head, _, stem = file_col.rpartition('/')
    return head, stem


def reconstruct(files, row_count):
    """The rows the legacy rule produces from per-file totals and a row
    count, or ManifestUnavailable when no chunk size yields that count
    (a capped attempt, or a definition changed since)."""
    if not files or not row_count:
        raise ManifestUnavailable(
            'no per-file event totals on the definition, or no row count '
            'on the attempt')
    totals = [max(int(n), 1) for _f, _e, n in files]

    def chunks_for(x):
        return [(-(-n // x)) for n in totals]

    lo, hi = 1, max(totals)
    while lo < hi:
        mid = (lo + hi) // 2
        if sum(chunks_for(mid)) > row_count:
            lo = mid + 1
        else:
            hi = mid
    counts = chunks_for(lo)
    if sum(counts) != row_count:
        raise ManifestUnavailable(
            f'no chunking of {len(files)} files with these totals yields '
            f'{row_count} rows; the attempt was cut or capped by hand')
    rows = []
    for (f, e, ntotal), nchunks in zip(files, counts):
        nevents = int(ntotal) // nchunks
        rows.extend((f, e, nevents, c) for c in range(nchunks))
    return rows


def verify(rows, delivered_keys, tails=None):
    """The reconstruction against what the attempt delivered: every
    delivered output in the attempt's input directories must name a file
    in the rows and a chunk below that file's count. Returns the list of
    violations (empty = verified)."""
    counts = {}
    for f, _e, _n, c in rows:
        key = _stem_key(f)
        counts[key] = max(counts.get(key, 0), c + 1)
    bad = []
    for head, stem, chunk in delivered_keys or ():
        if tails is not None and str(head).lower().strip('/') not in tails:
            continue
        n = counts.get((head, stem))
        if n is None:
            bad.append(f'delivered output for {stem} names no file in the rows')
        elif int(chunk) >= n:
            bad.append(f'{stem} chunk {chunk} delivered but the rows give '
                       f'{n} chunks')
        if len(bad) >= 5:
            break
    return bad


def consistency(rows_a, rows_b):
    a = set(format_rows(rows_a))
    b = set(format_rows(rows_b))
    return {'agree': a == b, 'rows_a': len(a), 'rows_b': len(b),
            'only_a': len(a - b), 'only_b': len(b - a)}


# ------------------------------------------------------------ the attempt

def choose_attempt(task, jedi_task_id=None):
    """The attempt a residual completes: the one named, else the attempt
    with the most rows (the latest of equals), read from its record or
    its task parameters."""
    rows = [r for r in task.panda_tasks.all() if r.jedi_task_id]
    if jedi_task_id is not None:
        for r in rows:
            if int(r.jedi_task_id) == int(jedi_task_id):
                return r
        raise ManifestUnavailable(
            f'PanDA task {jedi_task_id} is not an attempt of this task')
    best, best_key = None, None
    for r in rows:
        rec = record_of(r)
        n = rec['rows'] if rec else (attempt_row_count(r.jedi_task_id) or 0)
        key = (n, r.try_number)
        if best_key is None or key > best_key:
            best, best_key = r, key
    if best is None:
        raise ManifestUnavailable('the task records no PanDA attempt')
    return best


def attempt_manifest(task, panda_tasks, delivered_keys=None):
    """The rows of ``panda_tasks``' manifest and how they were established:
    the record, else the sandbox (recorded on success), else a verified
    reconstruction (recorded as such). The reconstruction is computed
    beside an exact manifest whenever the definition allows, for the
    consistency check. Returns (rows, info)."""
    info = {'attempt': {'panda_tasks_id': panda_tasks.pk,
                        'try_number': panda_tasks.try_number,
                        'jedi_task_id': panda_tasks.jedi_task_id,
                        'task_name': panda_tasks.task_name},
            'source': None, 'rows': 0, 'reconstruction': None,
            'consistency': None}
    rows, exact = None, False
    rec = record_of(panda_tasks)
    if rec:
        rows, exact = expand(rec), rec.get('source') != 'reconstructed'
        info['source'] = f"record ({rec.get('source')})"
    else:
        try:
            rows = fetch_from_cache(panda_tasks.jedi_task_id)
            store(panda_tasks, rows, 'cache')
            exact, info['source'] = True, 'sandbox in the PanDA cache'
        except ManifestUnavailable as e:
            info['sandbox'] = str(e)

    recon, recon_note = None, None
    tails = input_dirs(task, rows)
    files = definition_files(task, tails)
    try:
        recon = reconstruct(files, attempt_row_count(panda_tasks.jedi_task_id))
        bad = verify(recon, delivered_keys, tails)
        recon_note = 'verified' if not bad else 'rejected: ' + '; '.join(bad)
        if bad:
            recon = None
    except ManifestUnavailable as e:
        recon_note = f'unavailable: {e}'
    info['reconstruction'] = recon_note

    if rows is not None and recon is not None:
        info['consistency'] = consistency(rows, recon)
    if rows is None:
        if recon is None:
            raise ManifestUnavailable(
                f"the attempt's manifest could not be established: "
                f"{info.get('sandbox', 'no sandbox')}; reconstruction "
                f"{recon_note}. The attempt's job logs remain as evidence.")
        rows = recon
        store(panda_tasks, rows, 'reconstructed')
        info['source'] = 'reconstructed from the definition and the row count'
    info['rows'] = len(rows)
    info['exact'] = exact
    return rows, info


def to_json(info):
    return json.loads(json.dumps(info, default=str))
