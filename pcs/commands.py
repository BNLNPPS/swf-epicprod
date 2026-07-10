"""
Command generation for PCS production tasks.

Generates Condor (submit_csv.sh) and PanDA (prun) submission commands
from a fully specified ProdTask (Dataset + ProdConfig + overrides).

Reference repos:
- eic/job_submission_condor — Condor submission framework
- eic/simulation_campaign_hepmc3 — in-container execution pipeline
- eic/simulation_campaign_datasets — CSV input files
"""

import re
import shlex


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
    # Beam energies from physics tag
    env['EBEAM'] = str(physics.get('beam_energy_electron', ''))
    env['PBEAM'] = str(physics.get('beam_energy_hadron', ''))

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

    env = {
        'EBEAM': str(physics.get('beam_energy_electron', '')),
        'PBEAM': str(physics.get('beam_energy_hadron', '')),
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


def _evgen_manifest_from_inputs(task, events_per_job):
    """Resolve the task's matched JLab Rucio EVGEN DID(s) to per-job manifest
    rows ``file,ext,nevents,ichunk``.

    The matched DIDs live on the bound dataset's ``metadata['rucio']['matched']``
    (``task.inputs``), written by the EVGEN assimilation. A Rucio file's name IS
    the xrootd path below ``EVGEN/`` — the payload prepends
    ``root://…/volatile/eic/EPIC/`` to ``EVGEN/<file>`` and streams it; nothing
    is read from local disk. Rucio carries no per-file event count, so
    ``nevents`` is the configured per-job count and there is one job (row) per
    file (``ichunk`` 0). Resolution is a public ``eicread`` read of JLab Rucio.

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
            rows.append(f'{file_col},{ext},{events_per_job},0000')
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
        'EBEAM': str(physics.get('beam_energy_electron', '')),
        'PBEAM': str(physics.get('beam_energy_hadron', '')),
    }
    _add_background_env(env, task)
    return {k: v for k, v in env.items() if v != ''}


def build_evgen_task_params(task, panda_tasks=None):
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

    # Per-job manifest (file,ext,nevents,ichunk), one row per matched Rucio EVGEN
    # file; PanDA's %RNDM→${SEQNUMBER} selects the row in-job. nevents is the
    # configured per-job count (Rucio has none).
    n_events = int(data.get('events_per_job') or 0)
    if n_events <= 0:
        raise ValueError(
            'set events_per_job on the config (per-job event count; Rucio '
            'carries no per-file event count)')
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
        'site': cfg.get('panda_site') or 'BNL_OSG_PanDA_1',
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
    }
