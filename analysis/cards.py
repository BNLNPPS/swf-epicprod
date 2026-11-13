"""The cards of an analysis run, as pure functions (docs/ANALYSIS.md).

Four cards say what an analysis run did. The analysis card is the
analyst's, read from their repository at a pinned commit. The dataset
card is epicprod's frozen file list of one sample at one revision. The
run card is epicprod's statement of which analysis ran on which dataset
cards, minus which files, with which parameters and split; its sha256 is
the run's identity. The result card, written at publish, is the sidecar
of each plot.

Nothing here touches the database or the network, so the rules can be
tested on their own and read in one place. Django code (services.py)
supplies the rows and stores the results.
"""
import gzip
import hashlib
import io
import json
import re

RUN_CARD_SCHEMA = 1
DATASET_CARD_SCHEMA = 1

#: A diverted registration carries the attempt's mark after the chunk
#: and is another copy of the same work unit (pcs.commands.RECO_LFN_TAIL_RE).
DIVERTED_RE = re.compile(r'^(.*\.\d{4})\.p\w+(\.eicrecon\.edm4eic\.root)$')
TRY_SEGMENT_RE = re.compile(r'^try(\d+)$')
COMMIT_RE = re.compile(r'^[0-9a-f]{40}$')
SHA256_RE = re.compile(r'^[0-9a-f]{64}$')

#: Where a run publishes, under EIC-XRD-LOG's prefix (/work/eic3/EPIC/eic):
#: validation runs where Hydra reads, a user's own runs under their name.
PURPOSES = ('validation', 'user')
#: Stage keys an analysis card must name.
STAGES = ('map', 'merge', 'finalize')
#: Variables the payload sets for a stage command; nothing else is
#: substituted into a command line.
STAGE_VARIABLES = ('SAMPLE', 'JOB', 'PARAMS')
#: Paths an analysis card names, relative to its repository, with
#:  and  (and the card's parameters) substituted.
FILE_KEYS = ('list', 'partial', 'hists', 'plots')


class CardError(ValueError):
    """A card that cannot be used, with the reason."""


def canonical_json(obj):
    return json.dumps(obj, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=True)


def canonical_sha256(obj):
    """sha256 of canonical JSON: sorted keys, no whitespace. The identity
    rule for every card: equal content, equal digest, on any machine."""
    return hashlib.sha256(canonical_json(obj).encode()).hexdigest()


# --- analysis card ---------------------------------------------------------

def check_analysis_card(card):
    """The fields epicprod needs from an analysis card, checked at
    registration (cardio has no schema yet, so epicprod checks its own
    fields and leaves the rest alone). Returns the card unchanged or
    raises CardError naming every problem at once."""
    problems = []
    if not isinstance(card, dict):
        raise CardError('the card is not a mapping')
    if not str(card.get('identifier') or '').strip():
        problems.append('identifier is missing')
    shell = card.get('eic_shell')
    if not isinstance(shell, dict) or not shell.get('container') \
            or not shell.get('version'):
        problems.append('eic_shell needs container and version')
    stages = card.get('stages')
    if not isinstance(stages, dict):
        problems.append('stages is missing')
    else:
        for stage in STAGES:
            if not str(stages.get(stage) or '').strip():
                problems.append(f'stages.{stage} is missing')
    split = card.get('split') or {}
    if not isinstance(split, dict) or not _positive_int(split.get('files_per_job')):
        problems.append('split.files_per_job must be a positive integer')
    params = card.get('parameters', {})
    if not isinstance(params, dict):
        problems.append('parameters must be a mapping')
    else:
        bad = [k for k in params if not re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', str(k))]
        if bad:
            problems.append(f'parameter names must be shell names: {", ".join(map(str, bad))}')
    files = card.get('files') or {}
    missing = [k for k in FILE_KEYS if not isinstance(files, dict) or not files.get(k)]
    if missing:
        problems.append('files needs ' + ', '.join(f'files.{k}' for k in missing)
                        + ' (where a map job reads its file list and writes its partial, '
                        'and where finalize leaves the histogram contents and the plots)')
    for aux in card.get('auxiliary') or []:
        if not isinstance(aux, dict) or not (aux.get('path') or aux.get('url')) \
                or not SHA256_RE.match(str(aux.get('sha256') or '')):
            problems.append('each auxiliary entry needs a path or url, and a sha256')
            break
    if problems:
        raise CardError('; '.join(problems))
    return card


def _positive_int(value):
    try:
        return int(value) > 0 and int(value) == float(value)
    except (TypeError, ValueError):
        return False


def check_commit(commit):
    commit = str(commit or '').strip().lower()
    if not COMMIT_RE.match(commit):
        raise CardError(f'a full 40-character commit is needed, not {commit!r}')
    return commit


# --- dataset card ----------------------------------------------------------

def work_unit(did):
    """The work unit a RECO file delivers: its file name, with any
    diverted-registration mark removed. The same unit in two tries is
    the same work produced twice."""
    name = str(did).rstrip('/').rsplit('/', 1)[-1]
    return DIVERTED_RE.sub(r'\1\2', name)


def try_of(did):
    """The try a RECO DID was written by: its tryN path segment, else 1."""
    for part in str(did).strip('/').split('/'):
        m = TRY_SEGMENT_RE.match(part)
        if m:
            return int(m.group(1))
    return 1


def sample_of(did):
    """The sample directory of a RECO DID, without its try segment:
    /RECO/26.07.3/epic_craterlake/try2/SIDIS/.../f.root ->
    RECO/26.07.3/epic_craterlake/SIDIS/..."""
    parts = str(did).strip('/').split('/')[:-1]
    return '/'.join(p for p in parts if not TRY_SEGMENT_RE.match(p))


def manifest_rows(files):
    """Freeze a sample's delivered files into manifest rows: one file per
    work unit, the latest try winning, sorted by unit.

    ``files`` is an iterable of mappings with ``did`` and optionally
    ``unit`` (the producer's own segment key), ``try``, ``events``,
    ``bytes`` and ``adler32``. A file without a unit is keyed by
    ``work_unit(did)``; a file without a try by ``try_of(did)``. Two
    copies of a unit in the same try keep the lexically first DID, so the
    result never depends on input order.
    """
    best = {}
    for f in files:
        did = '/' + str(f['did']).strip('/')
        unit = str(f.get('unit') or '') or work_unit(did)
        attempt = int(f.get('try') or try_of(did))
        row = {
            'did': did,
            'unit': unit,
            'try': attempt,
            'events': _int_or_none(f.get('events')),
            'bytes': _int_or_none(f.get('bytes')),
            'adler32': (str(f['adler32']).lower().zfill(8)
                        if f.get('adler32') else None),
        }
        have = best.get(unit)
        if (have is None or attempt > have['try']
                or (attempt == have['try'] and did < have['did'])):
            best[unit] = row
    return [best[u] for u in sorted(best)]


def _int_or_none(value):
    try:
        return int(value) if value is not None and value != '' else None
    except (TypeError, ValueError):
        return None


def dataset_card(sample, revision, rows, source, frozen_at):
    """The header of a frozen file list. ``sample`` is the sample path
    (RECO/<version>/<config>/...), ``rows`` the manifest_rows. The
    manifest digest covers every row, so the card names its file list
    exactly while staying small."""
    if not rows:
        raise CardError(f'{sample}: no delivered files to freeze')
    samples = {sample_of(r['did']) for r in rows}
    if samples != {sample}:
        raise CardError(f'files of other samples in the card of {sample}: '
                        + ', '.join(sorted(samples - {sample})))
    events = [r['events'] for r in rows]
    return {
        'schema': DATASET_CARD_SCHEMA,
        'identifier': f'epic:/{sample}',
        'sample': sample,
        'revision': int(revision),
        'source': source,
        'file_count': len(rows),
        'events': sum(events) if all(e is not None for e in events) else None,
        'tries': sorted({r['try'] for r in rows}),
        'manifest_sha256': canonical_sha256(rows),
        'frozen_at': frozen_at,
    }


def manifest_blob(rows):
    """The manifest as stored and served: gzipped CSV, one row per file,
    with a fixed column order and a fixed gzip timestamp so equal rows
    give equal bytes."""
    out = io.StringIO()
    out.write('did,unit,try,events,bytes,adler32\n')
    for r in rows:
        out.write(','.join('' if r[k] is None else str(r[k])
                           for k in ('did', 'unit', 'try', 'events', 'bytes', 'adler32')) + '\n')
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode='wb', mtime=0) as gz:
        gz.write(out.getvalue().encode())
    return buf.getvalue()


def read_manifest_blob(blob):
    text = gzip.decompress(bytes(blob)).decode()
    lines = text.splitlines()
    header = lines[0].split(',')
    rows = []
    for line in lines[1:]:
        values = dict(zip(header, line.split(',')))
        rows.append({
            'did': values['did'], 'unit': values['unit'], 'try': int(values['try']),
            'events': _int_or_none(values['events']), 'bytes': _int_or_none(values['bytes']),
            'adler32': values['adler32'] or None,
        })
    return rows


# --- run card --------------------------------------------------------------

def campaign_of(sample):
    """RECO/26.07.3/... -> 26073: the campaign folder a run publishes to,
    the release with the dots dropped."""
    parts = str(sample).strip('/').split('/')
    if len(parts) < 2 or parts[0] != 'RECO':
        raise CardError(f'not a RECO sample path: {sample!r}')
    return parts[1].replace('.', '')


def sample_label(sample):
    """The name a run gives a sample when it passes it to the analysis
    ($SAMPLE): the last three path components, joined with '_'.
    RECO/.../ep_noradcor/9x130/x_1e-2to1e-1 -> ep_noradcor_9x130_x_1e-2to1e-1."""
    parts = str(sample).strip('/').split('/')
    return '_'.join(parts[-3:])


def run_card(tag, inputs, exclusions, parameters, split):
    """The run card: every choice that changes the result, and nothing
    that does not (queue, memory, owner, purpose and time stay out, so a
    rerun elsewhere or a promotion to validation keeps the id).

    ``tag``: {'label', 'repo', 'commit', 'card_path', 'card_sha256', 'image'}.
    ``inputs``: [{'sample', 'revision', 'manifest_sha256'}].
    ``exclusions``: DIDs left out. ``parameters``: the effective values.
    ``split``: {'files_per_job': N}.
    """
    for key in ('label', 'repo', 'commit', 'card_path', 'card_sha256', 'image'):
        if not tag.get(key):
            raise CardError(f'the analysis tag has no {key}')
    if not inputs:
        raise CardError('a run needs at least one dataset card')
    campaigns = {campaign_of(i['sample']) for i in inputs}
    if len(campaigns) != 1:
        raise CardError('a run reads one campaign; these inputs span '
                        + ', '.join(sorted(campaigns)))
    if not _positive_int(split.get('files_per_job')):
        raise CardError('split.files_per_job must be a positive integer')
    return {
        'schema': RUN_CARD_SCHEMA,
        'analysis': {k: tag[k] for k in ('label', 'repo', 'commit', 'card_path',
                                          'card_sha256', 'image')},
        'inputs': sorted(({'sample': i['sample'], 'revision': int(i['revision']),
                           'manifest_sha256': i['manifest_sha256']} for i in inputs),
                         key=lambda i: i['sample']),
        'exclusions': sorted({'/' + str(d).strip('/') for d in exclusions or []}),
        'parameters': dict(sorted((parameters or {}).items())),
        'split': {'files_per_job': int(split['files_per_job'])},
    }


def run_card_id(card):
    return canonical_sha256(card)


def effective_parameters(card_parameters, overrides):
    """The analysis card's defaults with the run's overrides on top; an
    override of a parameter the card does not declare is refused, so a
    typo cannot pass silently."""
    params = dict(card_parameters or {})
    unknown = sorted(set(overrides or {}) - set(params))
    if unknown:
        raise CardError('parameters the analysis card does not declare: '
                        + ', '.join(unknown))
    params.update(overrides or {})
    return params


def params_string(parameters):
    """The run's parameters as one shell word list, K=V sorted by name:
    the $PARAMS a stage command passes to its runner (for Snakemake,
    --config $PARAMS). Values must be single words."""
    words = []
    for key, value in sorted(parameters.items()):
        text = 'true' if value is True else 'false' if value is False else str(value)
        if not text or re.search(r'[\s\'"`$\\;&|<>()]', text):
            raise CardError(f'parameter {key}={text!r} is not a single plain word')
        words.append(f'{key}={text}')
    return ' '.join(words)


# --- job split -------------------------------------------------------------

def split_jobs(inputs, exclusions, files_per_job):
    """The map jobs of a run: per sample, its files minus the exclusions,
    in manifest order, cut into groups of ``files_per_job``. Jobs never
    mix samples, because the analysis is told which sample it reads.

    ``inputs``: [(sample, rows)] in run-card order. Returns
    [{'seq', 'sample', 'label', 'job', 'dids'}], seq counting from 0
    across the run (PanDA's %RNDM row), job counting from 0 within the
    sample (the analysis's $JOB, zero-padded to four digits)."""
    excluded = {'/' + str(d).strip('/') for d in exclusions or []}
    jobs = []
    for sample, rows in inputs:
        dids = [r['did'] for r in rows if r['did'] not in excluded]
        for k in range(0, len(dids), files_per_job):
            jobs.append({
                'seq': len(jobs),
                'sample': sample,
                'label': sample_label(sample),
                'job': f'{k // files_per_job:04d}',
                'dids': dids[k:k + files_per_job],
            })
    return jobs


def job_row(job):
    """A map job's manifest row: seq;label;job;did;did;... One line, no
    commas, so it travels in the per-task CSV the submission writes."""
    for did in job['dids']:
        if ';' in did or ',' in did or '\n' in did:
            raise CardError(f'unusable DID in a manifest row: {did!r}')
    return ';'.join([str(job['seq']), job['label'], job['job'], *job['dids']])


def parse_job_row(line):
    seq, label, job, *dids = line.strip().split(';')
    return {'seq': int(seq), 'label': label, 'job': job, 'dids': dids}


# --- storage ---------------------------------------------------------------

def publish_root(purpose, owner):
    """The folder under EIC-XRD-LOG's prefix a run publishes to."""
    if purpose not in PURPOSES:
        raise CardError(f'purpose must be one of {PURPOSES}, not {purpose!r}')
    if purpose == 'validation':
        return 'validation'
    if not re.match(r'^[a-z_][a-z0-9_.-]*$', str(owner or '')):
        raise CardError(f'unusable owner name for a folder: {owner!r}')
    return owner


def scratch_did(purpose, owner, run_id, name):
    """The unregistered DID name of a run's scratch file; an upload with
    this name lands at /work/eic3/EPIC/eic/<root>/scratch/<id12>/<name>."""
    return f'/{publish_root(purpose, owner)}/scratch/{run_id[:12]}/{name}'


def publish_did(purpose, owner, campaign, name):
    return f'/{publish_root(purpose, owner)}/images/campaigns/{campaign}/{name}'


def histogram_digests(hists):
    """Per histogram, its entries and the sha256 of its bin contents, from
    the {name: {'entries', 'bins'}} a finalize stage writes. Two runs
    match when these match; file checksums are no test, ROOT files and
    PNGs embed timestamps."""
    return {name: {'entries': h['entries'], 'bins_sha256': canonical_sha256(h['bins'])}
            for name, h in sorted(hists.items())}
