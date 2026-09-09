"""
Command generation for PCS production tasks.

Generates Condor (submit_csv.sh) and PanDA (prun) submission commands
from a fully specified ProdTask (Dataset + ProdConfig + overrides).

Reference repos:
- eic/job_submission_condor — Condor submission framework
- eic/simulation_campaign_hepmc3 — in-container execution pipeline
- eic/simulation_campaign_datasets — CSV input files
"""

import os
import re
import shlex

# Where an EVGEN task goes when its configuration names no site. Named here
# rather than written inline at the point of use so the page can show the
# reader the site a task would actually run at, and offer to change it.
EVGEN_DEFAULT_SITE = 'BNL_OSG_PanDA_1'


def _bg_param(task, key):
    """Background execution params prefer the k tag, with EvGen as fallback."""
    ds = task.dataset
    bg_params = {}
    if getattr(ds, 'background_tag_id', None) or getattr(ds, 'background_tag', None):
        bg_params = getattr(ds.background_tag, 'parameters', None) or {}
    if bg_params.get(key) not in (None, ''):
        return bg_params[key]
    evgen_params = getattr(ds.evgen_tag, 'parameters', None) or {}
    return evgen_params.get(key)


def _add_background_env(env, task, *, defaults=False):
    if not task.get_effective_config().get('bg_mixing'):
        return
    signal_freq = _bg_param(task, 'signal_freq')
    signal_status = _bg_param(task, 'signal_status')
    bg_tag_prefix = _bg_param(task, 'bg_tag_prefix')
    bg_files = _bg_param(task, 'bg_files')

    if defaults:
        env['SIGNAL_FREQ'] = str(signal_freq if signal_freq is not None else '0')
        env['SIGNAL_STATUS'] = str(signal_status if signal_status is not None else '0')
    else:
        if signal_freq is not None:
            env['SIGNAL_FREQ'] = str(signal_freq)
        if signal_status is not None:
            env['SIGNAL_STATUS'] = str(signal_status)
    if bg_tag_prefix:
        env['TAG_PREFIX'] = bg_tag_prefix
    if bg_files:
        env['BG_FILES'] = bg_files


def _add_try_env(env, panda_tasks):
    """Disambiguate payload-managed Rucio output paths for full task reruns."""
    try_number = int(getattr(panda_tasks, 'try_number', 0) or 0)
    if try_number <= 1:
        return
    suffix = f'try{try_number}'
    existing = str(env.get('TAG_PREFIX') or '').strip('/')
    env['TAG_PREFIX'] = f'{existing}/{suffix}' if existing else suffix


def build_condor_command(task):
    """
    Build the Condor submit_csv.sh command from a ProdTask.

    Produces the env-var-prefixed command used in the Colab notebook:
        EBEAM=... PBEAM=... scripts/submit_csv.sh osg_csv hepmc3 {csv} {hours}

    MOTHBALLED: the Condor submission path is no longer the production route —
    PanDA (the prun command and the taskParamMap) is. Kept for reference and the
    ``?fmt=condor`` artifact, but not maintained or used by the readiness/submit
    flow. Do not build new capability on it.
    """
    ds = task.dataset
    cfg = task.get_effective_config()
    physics = ds.physics_tag.parameters
    data = cfg.get('data') or {}

    env = {}
    # Beams from the physics tag, PBEAM carrying the ion isotope the
    # geometry name needs (payload_beams).
    env['EBEAM'], env['PBEAM'], _problem = payload_beams(task)

    # Detector from dataset
    env['DETECTOR_VERSION'] = ds.detector_version
    env['DETECTOR_CONFIG'] = ds.detector_config

    # Software stack from config
    env['JUG_XL_TAG'] = cfg.get('jug_xl_tag') or ''

    # Output flags
    env['COPYRECO'] = 'true' if cfg.get('copy_reco') else 'false'
    env['COPYFULL'] = 'true' if cfg.get('copy_full') else 'false'
    env['COPYLOG'] = 'true' if cfg.get('copy_log') else 'false'

    if cfg.get('use_rucio'):
        env['USERUCIO'] = 'true'
        env['X509_USER_PROXY'] = 'secrets/x509_user_proxy'

    # Rucio RSE override
    if cfg.get('rucio_rse'):
        env['OUT_RSE'] = cfg['rucio_rse']

    # External EVGEN input source (CSV manifest etc.)
    csv_path = task.input_source_location
    if csv_path:
        env['CSV_FILE'] = csv_path

    # Background mixing (conditional)
    _add_background_env(env, task, defaults=True)

    # Build env string (skip empty values)
    env_str = ' \\\n  '.join(f'{k}={v}' for k, v in env.items() if v)

    # Target hours from config (default 2)
    target_hours = cfg.get('target_hours_per_job') or 2

    csv = csv_path or '<csv_file>'
    cmd = f'scripts/submit_csv.sh osg_csv hepmc3 {csv} {target_hours}'

    return f'{env_str} \\\n  {cmd}'


def build_panda_command(task):
    """
    Build the PanDA prun command from a ProdTask.

    Produces prun arguments for PanDA submission. The actual submission
    uses PrunScript.main() from pandaclient, but this generates the
    equivalent CLI command for reference/execution.
    """
    ds = task.dataset
    cfg = task.get_effective_config()
    data = cfg.get('data') or {}

    parts = ['prun']

    # Exec command (the payload) — shlex.quote so inner quotes survive
    exec_cmd = data.get('exec_command', '')
    if exec_cmd:
        parts.append(f'--exec {shlex.quote(exec_cmd)}')

    # Output dataset — the produced dataset's true Rucio DID name (present
    # path-based convention; scope applied at submission). See
    # _output_dataset_name.
    parts.append(f'--outDS {_output_dataset_name(task)}')
    # Official group production (group.EIC scope, EIC.production privilege)
    if data.get('official'):
        parts.append('--official')

    # Container image
    container = cfg.get('container_image') or ''
    if container:
        parts.append(f'--containerImage {container}')

    # Site and queue
    if cfg.get('panda_site'):
        parts.append(f'--site {cfg["panda_site"]}')

    # Working group
    if cfg.get('panda_working_group'):
        parts.append(f'--workingGroup {cfg["panda_working_group"]}')

    # Resource type
    if cfg.get('panda_resource_type'):
        parts.append(f'--resourceType {cfg["panda_resource_type"]}')

    # Authorization
    prod_source = data.get('prod_source_label', 'test')
    parts.append(f'--prodSourceLabel {prod_source}')

    # VO
    vo = data.get('vo', 'wlcg')
    parts.append(f'--vo {vo}')

    # Processing type
    if data.get('processing_type'):
        parts.append(f'--processingType {data["processing_type"]}')

    # Job/event counts
    if data.get('n_jobs'):
        parts.append(f'--nJobs {data["n_jobs"]}')
    if data.get('events_per_job'):
        parts.append(f'--nEventsPerJob {data["events_per_job"]}')

    # Core count
    if data.get('corecount'):
        parts.append(f'--nCore {data["corecount"]}')

    # Flags
    if data.get('no_build'):
        parts.append('--noBuild')
    if data.get('skip_scout'):
        parts.append('--expertOnly_skipScout')

    # JEDI-managed outputs (simple payloads). Real production payloads that
    # self-register in Rucio use noOutput instead — then set neither.
    if data.get('outputs'):
        parts.append(f'--outputs {data["outputs"]}')

    return ' \\\n  '.join(parts)


def _build_env_string(task):
    """Shared env-var string for Condor command and JEDI jobParameters."""
    ds = task.dataset
    cfg = task.get_effective_config()
    physics = ds.physics_tag.parameters

    ebeam, pbeam, _problem = payload_beams(task)
    env = {
        'EBEAM': ebeam,
        'PBEAM': pbeam,
        'DETECTOR_VERSION': ds.detector_version,
        'DETECTOR_CONFIG': ds.detector_config,
        'JUG_XL_TAG': cfg.get('jug_xl_tag') or '',
        'COPYRECO': 'true' if cfg.get('copy_reco') else 'false',
        'COPYFULL': 'true' if cfg.get('copy_full') else 'false',
        'COPYLOG': 'true' if cfg.get('copy_log') else 'false',
    }
    # External EVGEN input source — payload-staged (see JEDI_INTEGRATION.md
    # § External EVGEN Inputs). The payload run.sh reads CSV_FILE and stages
    # the listed files at runtime.
    if task.input_source_location:
        env['CSV_FILE'] = task.input_source_location
    _add_background_env(env, task)
    return ' '.join(f'{k}={v}' for k, v in env.items() if v)


def build_task_dump(task):
    """
    Build a fully-resolved dict describing a ProdTask and everything it
    references: dataset, all four tags with parameters, the ProdConfig
    as stored, and the effective config (after task-level overrides).

    Suitable for human inspection or downstream tooling. Pure read.
    """
    ds = task.dataset
    cfg = task.prod_config

    def _tag(t):
        if t is None:
            return None
        return {
            'tag_label': t.tag_label,
            'tag_number': t.tag_number,
            'status': t.status,
            'description': t.description,
            'parameters': dict(t.parameters or {}),
            'created_by': t.created_by,
            'created_at': t.created_at.isoformat() if t.created_at else None,
        }

    def _cfg(c):
        if c is None:
            return None
        out = {}
        for f in c._meta.get_fields():
            if not hasattr(f, 'attname'):
                continue
            if f.name in ('id',):
                continue
            val = getattr(c, f.name, None)
            if hasattr(val, 'isoformat'):
                val = val.isoformat()
            out[f.name] = val
        return out

    effective = task.get_effective_config()
    for k, v in list(effective.items()):
        if hasattr(v, 'isoformat'):
            effective[k] = v.isoformat()

    return {
        'task': {
            'id': task.id,
            'composed_name': task.composed_name,
            'name': task.name,
            'description': task.description,
            'status': task.status,
            'csv_file': task.csv_file,
            'overrides': task.overrides or {},
            'input_dataset_dids': [d.did for d in task.input_datasets],
            'output_dataset_dids': [d.did for d in task.output_dataset_overrides],
            'intermediate_dataset_dids': [d.did for d in task.intermediate_datasets],
            'created_by': task.created_by,
            'created_at': task.created_at.isoformat() if task.created_at else None,
            'updated_at': task.updated_at.isoformat() if task.updated_at else None,
        },
        'dataset': {
            'id': ds.id,
            'composed_name': ds.composed_name,
            'dataset_name': ds.dataset_name,
            'did': ds.did,
            'scope': ds.scope,
            'detector_version': ds.detector_version,
            'detector_config': ds.detector_config,
            'blocks': ds.blocks,
            'description': ds.description,
            'created_by': ds.created_by,
            'created_at': ds.created_at.isoformat() if ds.created_at else None,
        },
        'tags': {
            'physics': _tag(ds.physics_tag),
            'evgen':   _tag(ds.evgen_tag),
            'simu':    _tag(ds.simu_tag),
            'reco':    _tag(ds.reco_tag),
        },
        'prod_config': _cfg(cfg),
        'effective_config': effective,
    }


def _output_dataset_name(task):
    """Output dataset name in the present (path-based) Rucio convention — the
    produced dataset's true DID name, scopeless::

        /RECO/<campaign>/<detector_config>/<suffix>

    Grounded in the catalog data, not assumed:

    - Stage RECO: the current campaign's recorded outputs are 100% RECO.
    - ``<campaign>`` is the task's production campaign (``Campaign.name``); the
      recorded produced version matches it (e.g. ``26.05.0``), not the input
      ``detector_version`` (``26.02.0``).
    - ``<suffix>`` is the requested EVGEN path with the ``/volatile/eic/EPIC/
      EVGEN/`` prefix stripped (case preserved). The path is the dataset's
      identity, so the RECO DID mirrors it per row — carrying the per-task
      angle/beam detail the tag composition collapses.

    Reconstructed from the task's own source path: deterministic, free of the
    lineage gather's over-matched siblings, and verified identical to the
    recorded true DID. The group.EIC scope is applied where the DID is formed
    (``out_dataset``/``log_dataset`` prepend ``ds.scope``). Falls back to the
    flat ``task_name`` when no EVGEN path or campaign is available.
    """
    ds = task.dataset
    parts = (ds.source_location or '').strip('/').split('/')
    if task.campaign_id and parts[:4] == ['volatile', 'eic', 'EPIC', 'EVGEN'] and len(parts) > 4:
        suffix = '/'.join(parts[4:])
        return f'/RECO/{task.campaign.name}/{ds.detector_config}/{suffix}'
    return ds.task_name


def build_task_params(task):
    """
    Build a JEDI ``taskParamMap`` dict from a ProdTask.

    The returned dict can be passed directly to
    ``pandaclient.Client.insertTaskParams()`` for JEDI submission.
    Pure mapping — no database writes, no network.

    Field mapping follows ``docs/JEDI_INTEGRATION.md``.
    """
    ds = task.dataset
    cfg = task.get_effective_config()
    data = cfg.get('data') or {}

    out_ds_name = _output_dataset_name(task)  # true Rucio DID name (dataset level)
    # LFN base built from the tag system — short, manageable filename control,
    # which is what tags are for. The dataset DID keeps the Rucio path-name and
    # LFNs are always resolved via Rucio, so a tag-based LFN preserves full
    # discoverability while staying short. Underscores per ePIC practice.
    # $PANDAID is substituted server-side per job (always — job_complex_module
    # line 3056) and is globally unique, so it makes each file LFN unique even
    # when datasets share the same tags (e.g. single-particle angle variants).
    tag_lfn = '_'.join(filter(None, [
        ds.physics_tag.tag_label, ds.evgen_tag.tag_label,
        ds.simu_tag.tag_label, ds.reco_tag.tag_label,
        ds.background_tag.tag_label if ds.background_tag_id else '',
    ]))
    working_group = cfg.get('panda_working_group') or 'EIC'

    params = {
        # Identity
        'taskName': out_ds_name,
        'userName': task.created_by,
        'vo': data.get('vo', 'eic'),
        'workingGroup': working_group,
        'campaign': ds.detector_version,

        # Processing
        'prodSourceLabel': data.get('prod_source_label', 'test'),
        'taskType': data.get('task_type', 'production'),
        'processingType': data.get('processing_type', 'epicproduction'),
        'taskPriority': data.get('task_priority', 900),

        # Executable (containerized)
        'transPath': data.get(
            'transformation',
            'https://pandaserver-doma.cern.ch/trf/user/runGen-00-00-02',
        ),
        'transUses': '',
        'transHome': '',
        'architecture': '',
        'container_name': cfg.get('container_image') or '',

        # Splitting (MC generation: noInput=True)
        'noInput': True,
        'nFilesPerJob': data.get('files_per_job', 1),
        'coreCount': data.get('corecount', 1),
        'ramCount': data.get('ram_count', 2000),
        'ramUnit': 'MBPerCore',

        # Site selection
        'site': cfg.get('panda_site') or '',
        'cloud': data.get('cloud', working_group),
    }

    # Job count — for noInput tasks this drives the number of jobs
    if data.get('n_jobs'):
        params['nFiles'] = data['n_jobs']
    if data.get('events_per_job'):
        params['nEventsPerJob'] = data['events_per_job']
    if cfg.get('events_per_task'):
        params['nEvents'] = cfg['events_per_task']

    # Walltime in seconds (JEDI expects seconds)
    hours = cfg.get('target_hours_per_job')
    if hours is not None:
        params['walltime'] = int(float(hours) * 3600)

    # Flags
    if data.get('skip_scout'):
        params['skipScout'] = True
    if data.get('disable_auto_retry'):
        params['disableAutoRetry'] = True
    if cfg.get('use_rucio'):
        params['useRucio'] = True

    # Output/log datasets carry the true Rucio DID name (scoped via ds.scope);
    # the LFN filename bases stay flat — slashes are not valid in an LFN.
    log_dataset = f'{ds.scope}:{out_ds_name}.log'
    out_dataset = f'{ds.scope}:{out_ds_name}'
    log_filename = f'{tag_lfn}.$PANDAID.log.${{SN}}.log.tgz'
    params['log'] = {
        'dataset': log_dataset,
        'type': 'template',
        'param_type': 'log',
        'token': 'local',
        'destination': 'local',
        'value': log_filename,
    }

    # jobParameters: env + exec command, then output template
    env_str = _build_env_string(task)
    exec_cmd = data.get('exec_command') or './run.sh'
    constant_value = f'{env_str} {exec_cmd}' if env_str else exec_cmd
    output_filename = f'{tag_lfn}.$PANDAID.${{SN}}.edm4eic.root'
    params['jobParameters'] = [
        {
            'type': 'constant',
            'value': constant_value,
        },
        {
            'type': 'template',
            'param_type': 'output',
            'token': 'local',
            'destination': 'local',
            'dataset': out_dataset,
            'value': output_filename,
            'offset': data.get('output_offset', 1000),
        },
    ]

    return params


# Known ePIC EVGEN file extensions, longest-match first. EVGEN filenames carry
# version dots (e.g. lAger3.6.1-1.0_jpsi_..._run1.hepmc3.tree.root), so the
# extension is a known multi-dot suffix, NOT everything after the first dot.
_EVGEN_EXTS = ('hepmc3.tree.root', 'hepmc.gz', 'hepmc3.gz', 'hepmc3', 'hepmc')


def _split_evgen_ext(basename):
    """Split an EVGEN filename into (stem, ext) by a known multi-dot suffix.
    Raises ValueError on an unrecognized extension — no silent last-dot guess,
    which would mangle a version-dotted name (lAger3.6.1-1.0_…)."""
    for ext in _EVGEN_EXTS:
        if basename.endswith('.' + ext):
            return basename[:-(len(ext) + 1)], ext
    raise ValueError(
        f'unrecognized EVGEN file extension: {basename!r} '
        f'(known: {", ".join(_EVGEN_EXTS)})')


def events_per_job_override(task):
    """A fixed events-per-job the task carries, or None: a hand-set
    ``overrides['max_events_per_job']``, else the ``MAX_EVENTS_PER_CHUNK``
    of the legacy line it was ingested from — the production team's
    bypass of the timing formula, honoured as an upper bound."""
    overrides = task.overrides or {}
    for value in (overrides.get('max_events_per_job'),
                  ((overrides.get('ingest') or {}).get('job') or {})
                  .get('MAX_EVENTS_PER_CHUNK')):
        try:
            n = int(str(value).strip())
        except (TypeError, ValueError):
            continue
        if n > 0:
            return n
    return None


def _chunk_rows(file_col, ext, file_events, events_per_job):
    """The manifest rows of one input file: one job when its events fit
    the per-job count, otherwise equal chunks — ceil(total / per-job)
    of them, each total // chunks events, the production team's
    chunking rule (csv_to_chunks.sh). Chunks are equal because the
    payload seeks to ``ichunk × nevents`` with the row's own count, so
    an unequal last chunk would read the wrong events. A file whose
    count is unknown is one job at the per-job count."""
    if not file_events or file_events <= events_per_job:
        nevents = file_events if file_events else events_per_job
        return [f'{file_col},{ext},{nevents},0000']
    chunks = -(-file_events // events_per_job)
    nevents = file_events // chunks
    return [f'{file_col},{ext},{nevents},{i:04d}' for i in range(chunks)]


EPIC_CONFIGURATIONS_DIR = os.environ.get(
    'EPIC_CONFIGURATIONS_DIR', '/data/wenauseic/github/epic/configurations')


def known_beam_geometries(detector_config):
    """The beam-specific geometries the detector repository defines for a
    configuration, ``{'10x100': ['', 'Au197'], '18x110': ['', 'Au', 'He3', ...]}``:
    the ion token each beam pair has a compact file for, the empty token
    the ep one. Read from the repository clone the nightly pull keeps;
    empty when the clone is absent, which the caller reports as unknown
    rather than as no geometry."""
    out = {}
    try:
        names = os.listdir(EPIC_CONFIGURATIONS_DIR)
    except OSError:
        return out
    prefix = f'{detector_config.replace("epic_", "")}_'
    for name in names:
        if not name.startswith(prefix) or not name.endswith('.yml'):
            continue
        body = name[len(prefix):-len('.yml')]
        beams, _, token = body.partition('_')
        if 'x' not in beams:
            continue
        out.setdefault(beams, []).append(token)
    return out


def payload_beams(task):
    """The EBEAM and PBEAM the payload composes its detector geometry from.

    The payload picks ``<config>_<EBEAM>x<PBEAM>.xml``, and for an ion beam
    the geometry name carries the isotope — ``10x100_Au197``, ``5x41_He3``
    — so PBEAM must, as the production team's lines do (``PBEAM=100_Au197``).
    A bare energy would select the electron-proton geometry, which exists,
    and the ion sample would simulate silently against the wrong one.

    The isotope comes, in order, from the line the edition was ingested
    from (its environment is on the edition), from ``overrides['ion_isotope']``
    set by hand, and from the detector repository's own geometry names for
    the beam pair when exactly one ion geometry of the species' element
    exists. Returns ``(ebeam, pbeam, problem)``; ``problem`` names what
    readiness must report when an ion beam's isotope cannot be settled,
    and PBEAM then carries the element so the payload refuses the job at
    its geometry check rather than running the wrong geometry.
    """
    ds = task.dataset
    physics = (ds.physics_tag.parameters or {}) if ds and ds.physics_tag_id else {}
    ebeam = str(physics.get('beam_energy_electron') or '').strip()
    energy = str(physics.get('beam_energy_hadron') or '').strip()
    species = str(physics.get('beam_species') or 'ep').strip()
    if species in ('', 'ep') or not species.startswith('e') or not energy:
        return ebeam, energy, ''
    element = species[1:]

    ingested = (((ds.metadata or {}).get('ingest') or {}).get('env') or {})
    line_pbeam = str(ingested.get('PBEAM') or '').strip()
    if '_' in line_pbeam and line_pbeam.split('_', 1)[0] == energy:
        return ebeam, line_pbeam, ''
    by_hand = str(((task.overrides or {}).get('ion_isotope')) or '').strip()
    if by_hand:
        return ebeam, f'{energy}_{by_hand}', ''
    geometries = known_beam_geometries(ds.detector_config)
    candidates = [t for t in geometries.get(f'{ebeam}x{energy}', [])
                  if t and t.startswith(element)]
    if len(candidates) == 1:
        return ebeam, f'{energy}_{candidates[0]}', ''
    if not geometries:
        problem = (f'Ion beam {species}: the isotope for the geometry is not '
                   f'recorded and the detector repository clone is not '
                   f'readable to look it up; set ion_isotope on the task '
                   f'(e.g. Au197).')
    elif candidates:
        problem = (f'Ion beam {species} at {ebeam}x{energy}: the detector '
                   f'repository has {len(candidates)} geometries '
                   f'({", ".join(candidates)}); set ion_isotope on the task.')
    else:
        problem = (f'Ion beam {species} at {ebeam}x{energy}: the detector '
                   f'repository defines no {element} geometry for this beam '
                   f'pair; set ion_isotope on the task if the image carries one.')
    return ebeam, f'{energy}_{element}', problem


def _evgen_manifest_from_inputs(task, events_per_job):
    """Resolve the task's matched JLab Rucio EVGEN DID(s) to per-job manifest
    rows ``file,ext,nevents,ichunk``.

    The matched DIDs live on the bound dataset's ``metadata['rucio']['matched']``
    (``task.inputs``), written by the EVGEN assimilation. A Rucio file's name IS
    the xrootd path below ``EVGEN/`` — the payload prepends
    ``root://…/volatile/eic/EPIC/`` to ``EVGEN/<file>`` and streams it; nothing
    is read from local disk. A file registered with its event count (every
    registration counts them now) is chunked against the per-job count; a
    file registered without one is one job at that count. Resolution is a
    public ``eicread`` read of JLab Rucio.

    Raises ValueError if the task has no matched input or it resolves to no
    files — a task with no real input must fail loudly, never submit empty.
    """
    from .services import fetch_jlab_rucio_did_files  # late import: avoid cycle
    matched = task.inputs
    if not matched:
        raise ValueError(
            'task has no matched Rucio EVGEN input (run the EVGEN matcher first)')
    rows = []
    for inp in matched:
        did = inp.get('did') or ''
        scope, sep, name = did.partition(':')
        if not sep:
            scope, name = 'epic', did
        for f in fetch_jlab_rucio_did_files(scope, name):
            fname = f.get('name') or ''
            rel = fname[len('/EVGEN/'):] if fname.startswith('/EVGEN/') else fname.lstrip('/')
            head, _, base = rel.rpartition('/')
            stem, ext = _split_evgen_ext(base)
            file_col = f'{head}/{stem}' if head else stem
            try:
                file_events = int(f.get('events') or 0)
            except (TypeError, ValueError):
                file_events = 0
            rows.extend(_chunk_rows(file_col, ext, file_events, events_per_job))
    if not rows:
        raise ValueError(
            'matched Rucio EVGEN DID(s) resolved to no files: '
            f'{[i.get("did") for i in matched]}')
    return rows


def _evgen_env(task):
    """The payload environment for the client-API path, as a dict.

    These are the ``osg_csv.sh.in`` keys (minus ``CSV_FILE``, which is the
    Condor convention — the client-API path carries inputs in the manifest,
    not this env — and minus ``X509_USER_PROXY``, injected by the doer when it
    copies the proxy into the sandbox, so no credential reference reaches the
    web tier). The payload's run.sh sources ``environment*.sh`` itself.
    """
    ds = task.dataset
    cfg = task.get_effective_config()
    data = cfg.get('data') or {}
    physics = ds.physics_tag.parameters
    env = {
        'COPYRECO': 'true' if cfg.get('copy_reco') else 'false',
        'COPYFULL': 'true' if cfg.get('copy_full') else 'false',
        'COPYLOG': 'true' if cfg.get('copy_log') else 'false',
        'USERUCIO': 'true' if cfg.get('use_rucio') else 'false',
        'OUT_RSE': cfg.get('rucio_rse') or '',
        'LOG_RSE': data.get('log_rse') or '',
        'DETECTOR_VERSION': ds.detector_version,
        'DETECTOR_CONFIG': ds.detector_config,
        'EBEAM': payload_beams(task)[0],
        'PBEAM': payload_beams(task)[1],
        # The beams whose detector geometry the job simulates with, when
        # they are not the physics tag's own: a trial's declared stand-in
        # for a beam pair the image has no compact file for.
        'DETECTOR_BEAMS': str(data.get('detector_beams') or ''),
    }
    _add_background_env(env, task)
    if str(data.get('workflow_mode') or 'external_evgen') == 'internal_evgen':
        env.update(_internal_evgen_env(task))
    return {k: v for k, v in env.items() if v != ''}


def _internal_evgen_env(task):
    """The generation environment of an internal-EVGEN task
    (docs/EPICPROD_INTERNAL_EVGEN.md § PCS): what the payload's
    evgen_generate.py composes the steering from, read from the tags
    and the production config. Every value the steering needs is here
    or the spec refuses: a job that starts without them would fail in
    its first minute for a reason known at submission."""
    ds = task.dataset
    cfg = task.get_effective_config()
    data = cfg.get('data') or {}
    physics = ds.physics_tag.parameters or {}
    evgen = ds.evgen_tag.parameters or {}
    process = str(physics.get('process') or '')
    needed = [
        ('physics tag process', process),
        ('physics tag beam_energy_electron', physics.get('beam_energy_electron')),
        ('physics tag beam_energy_hadron', physics.get('beam_energy_hadron')),
        ('evgen tag generator', evgen.get('generator')),
        ('evgen tag generator_version', evgen.get('generator_version')),
    ]
    # A Q^2 range is the DIS steering's phase space; an exclusive process
    # is steered by its state, mechanism and beam configuration instead,
    # passed through below when the tag carries them.
    if process.upper().startswith('DIS'):
        needed.append(('physics tag q2_range', physics.get('q2_range')))
    missing = [name for name, value in needed if not str(value or '').strip()]
    if missing:
        raise ValueError(
            'internal EVGEN needs ' + ', '.join(missing) + ' on the composed tags')
    env = {
        'EVGEN_INTERNAL': 'true',
        'EVGEN_GENERATOR': str(evgen.get('generator')),
        'EVGEN_GENERATOR_VERSION': str(evgen.get('generator_version')),
        'EVGEN_RADIATIVE': str(evgen.get('radiative') or 'off'),
        'EVGEN_PROCESS': process,
        'EVGEN_BEAM_SPECIES': str(physics.get('beam_species') or 'ep'),
        'EVGEN_AB_PRESET': str(data.get('afterburner_preset') or '1'),
        'COPYEVGEN': 'true' if data.get('copy_evgen') else 'false',
    }
    for key, name in (('q2_range', 'EVGEN_Q2_RANGE'), ('state', 'EVGEN_STATE'),
                      ('mechanism', 'EVGEN_MECHANISM'), ('channel', 'EVGEN_CHANNEL'),
                      ('x_range', 'EVGEN_X_RANGE'),
                      ('beam_config', 'EVGEN_BEAM_CONFIG')):
        if str(physics.get(key) or '').strip():
            env[name] = str(physics[key])
    return env


def internal_evgen_sample(task):
    """The generated sample's path below EVGEN/ and its file stem, in the
    production team's EVGEN layout, from the composed tags. DIS:
    ``DIS/pythia8.316-1.0/NC/noRad/ep/10x100/q2_10to100`` and
    ``pythia8.316-1.0_NC_noRad_ep_10x100_q2_10to100``. Any other
    process follows the exclusive samples' layout, the physics tag's
    category as the top directory and the tag's state, mechanism and
    beam_config (those present, in that order) as the qualifier:
    ``EXCLUSIVE/UPSILON/eSTARlight1.2.0/ep/9x275/1s_photo_hiAcc`` and
    ``eSTARlight1.2.0_UPSILON_1s_photo_hiAcc_ep_9x275``. The name a job
    generates under is this stem with ``_run<NNN>`` for its row."""
    ds = task.dataset
    physics = ds.physics_tag.parameters or {}
    evgen = ds.evgen_tag.parameters or {}
    process = str(physics.get('process') or '')
    # The generator token as the layout writes it, the inverse of the
    # catalog grammar (pcs/physics_match.py _split_gen_token): pythia8
    # with version 8.316-1.0 is 'pythia8.316-1.0', the major digit shared
    # by family and version; any other generator is name and version
    # joined as they came apart.
    gen_name = str(evgen.get('generator') or '')
    gen_version = str(evgen.get('generator_version') or '')
    m = re.fullmatch(r'pythia(\d)', gen_name.lower())
    if m and gen_version.startswith(m.group(1)):
        generator = f'pythia{gen_version}'
    else:
        generator = f'{gen_name}{gen_version}'
    species = str(physics.get('beam_species') or 'ep')
    beams = f"{physics.get('beam_energy_electron')}x{physics.get('beam_energy_hadron')}"
    if process.upper().startswith('DIS'):
        category, _, current = process.partition('_')
        current = current or 'NC'
        rad = 'noRad' if str(evgen.get('radiative') or 'off').lower() == 'off' else 'Rad'
        q2 = str(physics.get('q2_range') or '')
        path = '/'.join([category, generator, current, rad, species, beams, q2])
        stem = '_'.join([generator, current, rad, species, beams, q2])
        return path, stem
    area = str(ds.physics_tag.category.name or 'Exclusive').upper().replace(' ', '_')
    qualifier = '_'.join(str(physics[key])
                         for key in ('state', 'mechanism', 'channel', 'beam_config')
                         if physics.get(key))
    path = '/'.join([area, process.upper(), generator, species, beams]
                    + ([qualifier] if qualifier else []))
    stem = '_'.join([generator, process.upper()] + ([qualifier] if qualifier else [])
                    + [species, beams])
    return path, stem


def _evgen_manifest_internal(task, events_per_job):
    """One manifest row per job for an internal-EVGEN task: the sample
    the job generates, named as an external one would be, so the
    payload's naming and registration run unchanged. The job count is
    the config's ``n_jobs``; a trial takes the first row."""
    cfg = task.get_effective_config()
    data = cfg.get('data') or {}
    n_jobs = int(data.get('n_jobs') or 1)
    if n_jobs <= 0:
        raise ValueError('n_jobs on the config must be positive for internal EVGEN')
    path, stem = internal_evgen_sample(task)
    return [f'{path}/{stem}_run{i:03d},hepmc3.tree.root,{events_per_job},0000'
            for i in range(n_jobs)]


RECO_LFN_TAIL_RE = re.compile(r'\.(\d{4})\.eicrecon\.edm4eic\.root$')


def _delivered_row_keys(task):
    """The (relative-dir, stem, chunk) keys of every RECO file already
    delivered for this task across its attempts — registered in JLab
    Rucio and arrived, with an AVAILABLE replica on at least one RSE —
    with the DIDs checked and the per-DID arrival counts.

    Sources the task's recorded RECO outputs (``overrides['outputs']``
    entries and ``output_refs``, written by the lineage sweeps) and lists
    each against JLab Rucio. Arrival is judged first at the dataset level
    (one fast call: registered versus available file counts per RSE); a
    dataset complete on any RSE needs no file resolution, otherwise the
    files are resolved by DID in batches and those without an available
    replica anywhere are left out of the delivered set. The payload's
    naming convention (simulation_campaign_hepmc3 run.sh) writes each
    input row's RECO file to ``RECO/<ver>/<config>/<input dir below
    EVGEN>/<stem>.<chunk>.eicrecon.edm4eic.root``, so the key maps
    one-to-one onto manifest rows. Returns (None, [], {}) when the task
    records no RECO outputs — there is nothing to diff against, and the
    caller refuses.
    """
    from .services import (fetch_jlab_rucio_did_files,
                           fetch_jlab_rucio_dataset_arrival,
                           fetch_jlab_rucio_unarrived_files)
    overrides = task.overrides or {}
    dids = []
    for entry in (overrides.get('outputs') or []):
        if str(entry.get('stage', '')).upper() == 'RECO' and entry.get('did'):
            dids.append(entry['did'])
    for ref in (overrides.get('output_refs') or []):
        if str(ref.get('stage', '')).upper() == 'RECO' and ref.get('did'):
            dids.append(ref['did'])
    dids = sorted(set(dids))
    if not dids:
        return None, [], {}
    keys = set()
    arrival = {}
    for did in dids:
        scope, sep, name = did.partition(':')
        if not sep:
            scope, name = 'epic', did
        names = [str(f.get('name') or '')
                 for f in fetch_jlab_rucio_did_files(scope, name)]
        summary = fetch_jlab_rucio_dataset_arrival(scope, name)
        complete = any(
            r.get('length') is not None
            and r.get('available_length') == r.get('length')
            for r in summary)
        unarrived = (set() if complete
                     else fetch_jlab_rucio_unarrived_files(scope, names))
        arrival[did] = {'registered': len(names),
                        'unarrived': len(unarrived),
                        'rses': summary}
        for fname in names:
            if fname in unarrived:
                continue
            m = RECO_LFN_TAIL_RE.search(fname)
            if not m:
                continue
            chunk = m.group(1)
            base = fname[:m.start()]
            parts = [p for p in base.split('/') if p]
            # /RECO/<ver>/<config>/<subpath...>/<stem>
            if len(parts) < 4 or parts[0] != 'RECO':
                continue
            head = '/'.join(parts[3:-1])
            stem = parts[-1]
            keys.add((head, stem, chunk))
    return keys, dids, arrival


def _residual_rows(task, csv_rows, delivered=None):
    """The manifest rows whose RECO output is not delivered — not
    registered, or registered without an available replica — with the
    coverage record. ``delivered`` is the precomputed
    ``_delivered_row_keys`` result when the caller already has it.
    Raises ValueError with the refusal reason when the residual cannot
    be established honestly (design: JEDI_INTEGRATION.md § Residual
    rerun)."""
    env = _evgen_env(task)
    if env.get('TAG_PREFIX'):
        raise ValueError(
            'residual rerun is not supported for background-mixed tasks '
            '(TAG_PREFIX changes the output path shape); rerun the '
            'entire task instead')
    keys, dids, arrival = (delivered if delivered is not None
                           else _delivered_row_keys(task))
    if keys is None:
        raise ValueError(
            'no recorded RECO outputs to diff against — run the Rucio '
            'update first, or rerun the entire task')
    residual = []
    for row in csv_rows:
        file_col, _ext, _nev, chunk = row.rsplit(',', 3)
        head, _, stem = file_col.rpartition('/')
        if (head, stem, chunk) not in keys:
            residual.append(row)
    files_registered = sum(a['registered'] for a in arrival.values())
    files_unarrived = sum(a['unarrived'] for a in arrival.values())
    if not residual:
        raise ValueError(
            f'zero residual: all {len(csv_rows)} manifest rows have '
            f'RECO outputs registered and arrived at JLab — nothing to '
            f'rerun')
    return residual, {
        'rows_total': len(csv_rows),
        'rows_residual': len(residual),
        'checked_dids': dids,
        'files_registered': files_registered,
        'files_unarrived': files_unarrived,
        'arrival': arrival,
    }


def build_evgen_task_params(task, panda_tasks=None, residual=False,
                            residual_of=None):
    """Build the client-API EVGEN production submission spec from a ProdTask.

    This is the production reproduction of the proven condor-side recipe
    (eic/job_submission_condor submit_csv.sh + submit_panda_api.py, spec only):
    a noInput+noOutput PanDA task whose containerized payload xrootd-streams the
    EVGEN input from JLab and self-registers RECO to JLab Rucio. PCS is the
    single source of truth for the task definition; this returns the high-level
    spec the submit-evgen-task doer turns into the taskParamMap + sandbox under
    the prod-ops agent's credentials. The doer owns the submission kernel; the
    web tier holds no credential and builds no taskParamMap. See
    docs/JEDI_INTEGRATION.md § External EVGEN Inputs.

    Pure mapping — no DB writes, no network. Raises ValueError if the task has
    no EVGEN input bound (a misconfigured task must fail loudly).
    """
    ds = task.dataset
    cfg = task.get_effective_config()
    data = cfg.get('data') or {}

    residual_coverage = None
    if residual:
        # Residual rerun: the workload is the undelivered remainder of the
        # attempt being completed, over the rows that attempt actually ran
        # (pcs/manifests.py; JEDI_INTEGRATION.md § Residual rerun). Each
        # row carries its own nevents and ichunk, so nothing is asked of
        # the configuration's per-job event count. Refusal reasons
        # propagate as ValueError.
        from . import manifests
        delivered = _delivered_row_keys(task)
        if delivered[0] is None:
            raise ValueError(
                'no recorded RECO outputs to diff against — run the Rucio '
                'update first, or rerun the entire task')
        try:
            attempt = manifests.choose_attempt(task, residual_of)
            rows, info = manifests.attempt_manifest(
                task, attempt, delivered_keys=delivered[0])
        except manifests.ManifestUnavailable as e:
            raise ValueError(str(e))
        csv_rows, residual_coverage = _residual_rows(
            task, manifests.format_rows(rows), delivered)
        residual_coverage['manifest'] = manifests.to_json(info)
    else:
        # Per-job manifest (file,ext,nevents,ichunk), one row per job over
        # the matched Rucio EVGEN files; PanDA's %RNDM→${SEQNUMBER} selects
        # the row in-job. The per-job count is the config's events_per_job,
        # bounded by the task's own override where it carries one.
        n_events = int(data.get('events_per_job') or 0)
        if n_events <= 0:
            # No configured count: the measured cost of a trial of this
            # edition and the config's target job length give it, by the
            # production team's own formula.
            from .services import events_per_job_from_cost
            n_events = events_per_job_from_cost(
                task.dataset, cfg.get('target_hours_per_job')) or 0
        override = events_per_job_override(task)
        if override:
            n_events = min(n_events, override) if n_events > 0 else override
        if n_events <= 0:
            raise ValueError(
                'set events_per_job on the config, max_events_per_job on the '
                'task, or run a trial so the cost gives the per-job count')
        if str(data.get('workflow_mode') or 'external_evgen') == 'internal_evgen':
            # Internal EVGEN (docs/EPICPROD_INTERNAL_EVGEN.md): the job
            # generates its own sample, so the manifest names the sample
            # each job would have read, one row per job, and the
            # generation environment travels with the payload environment.
            csv_rows = _evgen_manifest_internal(task, n_events)
        else:
            csv_rows = _evgen_manifest_from_inputs(task, n_events)

    # The composed PCS identity is the logical campaign task. The physical
    # PanDA task/outDS name is attempt-specific when this is a retry or site race
    # (try2, try3, ...), carried by the PandaTasks association row.
    env = _evgen_env(task)
    _add_try_env(env, panda_tasks)
    out_ds = (
        getattr(panda_tasks, 'task_name', '') or
        task.composed_name or ds.composed_name or ds.build_dataset_name()
    )

    # Container: an explicit image wins; else build the cvmfs eic_xl ref from the
    # jug_xl tag, as submit_csv.sh does.
    container = cfg.get('container_image') or ''
    if not container and cfg.get('jug_xl_tag'):
        container = f"/cvmfs/singularity.opensciencegrid.org/eicweb/eic_xl:{cfg['jug_xl_tag']}"
    if not container:
        raise ValueError(
            'no container image (set container_image or jug_xl_tag on the config)')

    # csv_base: a filesystem-safe stem for the per-task manifest in the sandbox.
    csv_base = re.sub(r'[^A-Za-z0-9._-]', '_', out_ds) or 'evgen_input'

    hours = cfg.get('target_hours_per_job')
    walltime_hours = float(hours) if hours is not None else float(data.get('walltime_hours', 2.0))

    return {
        'outDS': out_ds,
        'vo': data.get('vo', 'epic'),
        'userName': task.created_by,
        'workingGroup': cfg.get('panda_working_group') or 'EIC',
        # A trial's site is the destination it was fired at to qualify, so it
        # outranks the configuration's: a trial that records GREX and submits
        # to OSG qualifies the wrong path and reports success for it.
        'site': (str((ds.metadata or {}).get('trial_site') or '')
                 or cfg.get('panda_site') or EVGEN_DEFAULT_SITE),
        'prodSourceLabel': data.get('prod_source_label', 'test'),
        'taskType': data.get('task_type', 'prod'),
        'processingType': data.get('processing_type', 'epicproduction'),
        'containerImage': container,
        'nCore': int(data.get('corecount', 1)),
        'memory': int(data.get('ram_count', 4096)),
        'disk': int(data.get('disk_count', 4096)),
        'walltimeHours': walltime_hours,
        # Scouts default OFF on this path during commissioning (Torre: "this is a
        # test") — skipScout uses walltime directly and avoids the noInput
        # pseudo-input HS06 brokerage pitfall. A config can flip it on later.
        'skipScout': bool(data.get('skip_scout', True)),
        'nJobs': len(csv_rows),
        'nEventsPerJob': 1,
        'exec': f'python3 evgen_job_dispatcher.py %RNDM=0 {csv_base}',
        'csvBase': csv_base,
        'csvRows': csv_rows,
        'env': env,
        'residual': residual_coverage,
        # A trial submits as a trial because it is one: its settings ride
        # on the spec so no caller has to remember a flag, and the Submit
        # button on a trial task does the right thing unchanged
        # (docs/PCS.md, Trials).
        **_trial_spec(ds),
    }


def _trial_spec(ds):
    """The trial settings of a trial's dataset, or nothing at all.

    The trial number and its settings live in the dataset's metadata and
    the composed name is derived from them, so this reads the record
    rather than parsing the name.
    """
    from .trials import trial_output_root  # late import: avoid cycle
    md = (ds.metadata or {}) if ds is not None else {}
    number = md.get('trial')
    if not number:
        return {}
    return {'trial': {
        'number': int(number),
        'events': int(md.get('trial_events') or 100),
        'outputRoot': str(md.get('trial_output_root')
                          or trial_output_root(ds.composed_name)),
        'lifetimeDays': int(md.get('trial_lifetime_days') or 14),
        'site': str(md.get('trial_site') or ''),
    }}
