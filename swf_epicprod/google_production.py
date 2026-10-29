"""Finite, replenished particle-gun production at Google's five-worker cap."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from urllib.parse import urlsplit
import uuid
import zlib

QUEUE = 'BNL_ePIC_GOOGLE_es'
SOURCE = 8524
# Multi-hour jobs (Torre, 2026-10-26): each job is sized past what a pod
# simulates within the harness ceiling, so it runs to the ceiling; three
# jobs per task keep the five workers fed from the two committed tasks.
EVENTS = 1800000
EVENTS_PER_JOB = 600000
MARKER = 'google_production'
MODULUS = 2147483647
AUTHORITY = '01a174ee-0d98-7656-a327-ddb85cde212d'
# The feeder's samples add statistics; a person's particle-gun request is
# production proper and runs first (Torre Wenaus, 2026-10-29). Samples
# submit below every priority level (pcs.commands.TASK_PRIORITY_BY_LEVEL),
# and none is added while a request is unfinished on Google.
SAMPLE_TASK_PRIORITY = 800
# A request runs as one job per pod.
REQUEST_JOBS = 5
# Resident simulation and units (EPICPROD_PAYLOAD.md, Resident Event
# Service units); the job declares the pod's fourteen payload cores.
# Continuous simulation as on Perlmutter: reconstruction runs off the
# slots' critical path on three shared EICrecon members, on the CPU the
# pod's limit gives above its fourteen-core request (GKE_PILOT_FLOW.md).
# Ranges of ten events (25 to a unit), as on Perlmutter: a tenth of the
# range rows, server reports and event-log lines; the pods' batch pilot
# counts a unit's events, not its ranges (runtime 18e2802 on).
SHAPE = dict(unit_events=250, range_events=10, batch_channel=True,
             close_s=300, deadline_s=14100,
             margin_s=600, memory_mb_per_core=6000, slots='auto', pilot_cores=14,
             resident_sim=True, resident_unit=True, fast_merge=True,
             report_barrier=True, reco_pool=3, reco_async=True)


def seed_intervals(stem, count):
    """All possible recovered range-start seeds, including the zero -> one map."""
    start = (int(hashlib.sha256(stem.encode()).hexdigest()[:7], 16) * 10000 + 1) % MODULUS
    end = start + count - 1
    intervals = ([(start, end)] if end < MODULUS
                 else [(start, MODULUS - 1), (0, end % MODULUS)])
    return [(max(1, low), max(1, high)) for low, high in intervals]


def overlaps(first, second):
    return any(a <= d and c <= b for a, b in first for c, d in second)


def reserve_sample():
    """Use PCS as the durable seed ledger, retaining completed and paused work."""
    return reserve('gOP', EVENTS, dict(SHAPE, events_per_job=EVENTS_PER_JOB), QUEUE, MARKER, AUTHORITY)


def plan_names(task, sample):
    """The generation plan's Rucio file and dataset for a gun task's sample:
    ``group.EIC.gunplan.<version>.<physics tag>.<sample>.json`` in
    ``group.EIC:group.EIC.esplan.<version>.<physics tag>.<sample>``."""
    ds = task.dataset
    stem = f'{ds.detector_version}.{ds.physics_tag.tag_label}.{sample}'
    return 'group.EIC.gunplan.' + stem + '.json', 'group.EIC:group.EIC.esplan.' + stem


def reserve(prefix, events, shape, site, marker, authority):
    """One sample of a feeder's fresh particle-gun events: its seeds disjoint
    from the source production and every ES sample on any queue, under the
    source task's row lock, which serializes the feeders."""
    from django.db import transaction
    from pcs.commands import internal_evgen_sample, prodtask_events_per_job
    from pcs.models import ProdTask
    from pcs.production_samples import compose
    with transaction.atomic():
        source = ProdTask.objects.select_for_update().get(pk=SOURCE)
        path, original_stem = internal_evgen_sample(source)
        chunks = -(-int(source.dataset.expected_events) // prodtask_events_per_job(source))
        reserved = seed_intervals(original_stem, chunks)
        samples = set()
        for task in ProdTask.objects.filter(overrides__has_key='event_service'):
            es = task.overrides['event_service']
            if es.get('input') == 'evgen':
                # An Event Service task over EVGEN files (evgen_es.py) takes
                # no gun seeds; read as a gun row it stopped every feeder's
                # reservation from 28 October, 09:13 ET (KeyError 'row').
                continue
            stem = es['row'].split(',')[0].rsplit('/', 1)[1]
            reserved.extend(seed_intervals(stem, int(es['events'])))
            samples.add(task.overrides.get('production_sample_prefix'))
        for index in range(1, 1000000):
            sample = f'{prefix}{index:06d}'
            stem = original_stem + '.' + sample
            seeds = seed_intervals(stem, events)
            if (sample not in samples and sum(b - a + 1 for a, b in seeds) == events
                    and not overlaps(seeds, reserved)):
                break
        else:
            raise RuntimeError(f'No disjoint {prefix} production seed interval remains')
        es = dict(shape, events=events, input_events=events,
                  input_dataset=plan_names(source, sample)[1],
                  row=f'{path}.{sample}/{stem},steer,{events},0000')
        task = compose(source, sample=sample, event_service=es, site=site,
                       created_by='wenaus')
        task.overrides[marker] = {'authority': authority, 'sample': sample,
                                  'events': events, 'seed_stem': stem}
        task.overrides['task_priority'] = SAMPLE_TASK_PRIORITY
        task.save(update_fields=['overrides'])
        return task


def adopt_request(task, *, by, reason, authority):
    """Run a person's particle-gun request on Google, ahead of the feeder's
    samples. The request keeps its identity and output name and takes
    Google's Event Service form, one job per pod; it leaves the front's
    ready pool, is placed on Google, and ``replenish`` submits it at its
    next cycle. ``authority`` names the operator's dialog turn."""
    from monitor_app.epicprod_logging import log_epicprod_action
    from pcs import services
    from pcs.commands import internal_evgen_sample, is_particle_gun
    from .production_es import definition
    ds = task.dataset
    if not is_particle_gun(task):
        raise ValueError('only a particle-gun request runs in this form')
    if task.panda_task_id or task.panda_tasks.exists():
        raise ValueError('the request is already in PanDA; started work stays where it is')
    if (ds.metadata or {}).get('trial'):
        raise ValueError('a trial runs where it was fired')
    events = int(ds.expected_events or 0)
    if events <= 0 or events % REQUEST_JOBS:
        raise ValueError(f'{events} expected events do not divide into {REQUEST_JOBS} jobs')
    path, stem = internal_evgen_sample(task)
    es = definition(dict(SHAPE, events_per_job=events // REQUEST_JOBS, events=events,
                         input_events=events, input_dataset=plan_names(task, ds.sample_name)[1],
                         row=f'{path}/{stem},steer,{events},0000'))
    overrides = dict(task.overrides or {})
    queued = overrides.pop('queued', None)
    overrides.update(event_service=es, copy_reco=True, copy_full=False, copy_log=False,
                     use_rucio=True)
    overrides[MARKER] = {'authority': authority, 'sample': ds.sample_name, 'events': events,
                         'seed_stem': stem, 'request': True, 'by': by, 'reason': reason}
    task.overrides = overrides
    task.save(update_fields=['overrides'])
    services.prodtask_place(task, QUEUE, changed_by=by)
    log_epicprod_action('pcs', 'google_request_adopted', username=by,
                        subject_type='campaign_task', subject_key=task.composed_name,
                        outcome='ok', sublevel='high', live_default=True, reason=reason,
                        message=(f'{task.composed_name} runs on Google ahead of its gun samples: '
                                 f'{events:,} events in {REQUEST_JOBS} jobs; {reason}'),
                        events=events, jobs=REQUEST_JOBS, authority=authority,
                        previous_site=(queued or {}).get('site') or '')
    return task


def requests(marker=MARKER):
    """The requests adopted for the feeder's queue, oldest first."""
    from pcs.models import ProdTask
    return [t for t in ProdTask.objects.filter(overrides__has_key=marker).order_by('pk')
            if ((t.overrides or {}).get(marker) or {}).get('request')]


def register_plan(task, proxy, marker=MARKER):
    """Idempotently stage the generation ledger through the proven BNL path."""
    # GSI refuses the group-readable service proxy. Keep its private copy
    # scoped to this upload, as in the validated commissioning submitter.
    keys = ('X509_USER_PROXY', 'X509_CERT_DIR')
    previous = {key: os.environ.get(key) for key in keys}
    try:
        with tempfile.TemporaryDirectory(prefix='google-proxy-', dir='/data/swf-tmp') as temp:
            private = Path(temp) / 'proxy'
            private.write_bytes(Path(proxy).read_bytes())
            private.chmod(0o600)
            _register_plan(task, str(private), marker)
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _register_plan(task, proxy, marker):
    os.environ.update(X509_USER_PROXY=proxy,
                      X509_CERT_DIR='/etc/grid-security/certificates')
    from django.conf import settings
    from rucio.client import Client
    from rucio.common.exception import DataIdentifierAlreadyExists, DataIdentifierNotFound
    from XRootD import client as xrd
    from pcs.commands import GUN_DEFAULT_BEAMS, gun_config
    entry = task.overrides[marker]
    es = task.overrides['event_service']
    sample = entry['sample']
    ds = task.dataset
    gun = gun_config(task)
    # The gun as the job's steering takes it (pcs.commands._gun_evgen_env).
    angles = ({'eta': float(gun['eta'])} if 'eta' in gun else
              {'theta_min_deg': int(gun['theta_min']), 'theta_max_deg': int(gun['theta_max'])})
    data = task.get_effective_config().get('data') or {}
    plan = dict(schema='epicprod-particle-gun-generation-plan-v1', events=entry['events'],
                sample=sample,
                source_task=((ds.metadata or {}).get('production_sample') or {}).get('source')
                or task.composed_name,
                generator='particle_gun', particle=gun['particle'], gun_energy=gun['energy_g4'],
                **angles, detector_version=ds.detector_version,
                detector_config=ds.detector_config,
                detector_beams=str(data.get('detector_beams') or GUN_DEFAULT_BEAMS),
                seed_stem=entry['seed_stem'], authority=entry['authority'],
                seed_scheme='(sha256(stem)[:7] * 10000 + zero_based_range_start + 1) modulo 2147483647; zero maps to one',
                event_number='zero-based absolute event position in this plan')
    data = (json.dumps(plan, indent=2) + '\n').encode()
    adler = f'{zlib.adler32(data) & 0xffffffff:08x}'
    lfn = plan_names(task, sample)[0]
    dataset = es['input_dataset'].split(':', 1)[1]
    catalog = Client(rucio_host=settings.RUCIO_BNL_URL, auth_host=settings.RUCIO_BNL_URL,
                     account=settings.RUCIO_BNL_ACCOUNT, vo=settings.RUCIO_BNL_VO,
                     auth_type='x509_proxy', creds={'client_proxy': proxy}, ca_cert=None,
                     timeout=40)
    try:
        meta = catalog.get_metadata('group.EIC', lfn)
        if meta['bytes'] != len(data) or meta['adler32'] != adler:
            raise RuntimeError('The reserved generation plan differs in Rucio')
    except DataIdentifierNotFound:
        pfn = catalog.lfns2pfns('BNL_PROD_DISK_1', ['group.EIC:' + lfn],
                               operation='write', scheme='root')['group.EIC:' + lfn]
        parsed = urlsplit(pfn)
        fs = xrd.FileSystem(f'{parsed.scheme}://{parsed.netloc}')
        status, info = fs.stat(parsed.path, timeout=40)
        if status.ok:
            check, checksum = fs.query(xrd.flags.QueryCode.CHECKSUM, parsed.path, timeout=40)
            if info.size != len(data) or not check.ok or adler.encode() not in checksum:
                raise RuntimeError('The reserved generation plan differs on storage')
        elif status.errno == 3011:
            with tempfile.TemporaryDirectory(prefix='google-plan-', dir='/data/swf-tmp') as temp:
                path = Path(temp) / 'plan.json'
                path.write_bytes(data)
                env = dict(os.environ, X509_USER_PROXY=proxy,
                           X509_CERT_DIR='/etc/grid-security/certificates')
                subprocess.run(['xrdcp', '--cksum', 'adler32:' + adler, str(path), pfn],
                               check=True, timeout=120, env=env)
        else:
            raise RuntimeError(str(status))
        catalog.add_replicas('BNL_PROD_DISK_1', [{'scope': 'group.EIC', 'name': lfn,
            'bytes': len(data), 'adler32': adler,
            'meta': {'guid': uuid.uuid4().hex, 'events': entry['events']}}])
    try:
        catalog.add_dataset('group.EIC', dataset)
    except DataIdentifierAlreadyExists:
        pass
    files = list(catalog.list_files('group.EIC', dataset))
    if not files:
        catalog.attach_dids('group.EIC', dataset, [{'scope': 'group.EIC', 'name': lfn}])
    elif len(files) != 1 or files[0]['name'] != lfn:
        raise RuntimeError('Generation dataset has unexpected files')
    if not list(catalog.list_did_rules('group.EIC', dataset)):
        catalog.add_replication_rule([{'scope': 'group.EIC', 'name': dataset}], 1, 'BNL_PROD_DISK_1')
    if catalog.get_metadata('group.EIC', dataset).get('open'):
        catalog.close('group.EIC', dataset)


def task_inventory(marker=MARKER, label='Google'):
    """Unknown, paused, failed or incomplete tasks hold replenishment."""
    from django.db import connections
    from pcs.models import ProdTask
    active, draft = [], None
    for task in ProdTask.objects.filter(overrides__has_key=marker).order_by('pk'):
        entry = (task.overrides or {}).get(marker)
        if isinstance(entry, dict) and entry.get('reviewed'):
            continue  # an operator reviewed it and released the supply
        attempt = task.panda_tasks.order_by('-try_number').first()
        taskid = task.panda_task_id or (attempt.jedi_task_id if attempt else None)
        if taskid:
            with connections['panda'].cursor() as cursor:
                cursor.execute('SELECT status FROM doma_panda.jedi_tasks WHERE jeditaskid=%s', [taskid])
                row = cursor.fetchone()
            if not row:
                raise RuntimeError(f'No live PanDA state for {label} task {taskid}')
            state = row[0]
            if state == 'done':
                continue
            if state in ('finished', 'failed', 'broken', 'aborted', 'exhausted', 'paused'):
                raise RuntimeError(f'{label} task {taskid} is {state}; operator review required')
            active.append(taskid)
        elif attempt:
            active.append(task.pk)
        elif draft is None:
            draft = task
    return active, draft


def replenish(cfg):
    """Called under the existing GKE controller lock, at most one submit/cycle."""
    from monitor_app.models import SysConfig
    if not SysConfig.get_setting('google_production.enabled', False):
        return {'outcome': 'disabled'}
    from monitor_app.cached_product import get_product
    from monitor_app.epicprod_logging import log_epicprod_action
    result = {'outcome': 'held', 'queue': QUEUE}
    try:
        from .queues import entries
        if (cfg['queue'] != QUEUE or not cfg['enabled'] or int(cfg['max_pods']) != 5
                or int(cfg['cpu']) != 14):
            raise RuntimeError('Google operation requires the authorized five 14-core workers')
        entry = next(e for e in entries() if e['queue'] == QUEUE)
        if entry['status'] != 'production':
            raise RuntimeError('Google queue is not in production')
        active, task = task_inventory()
        result['active_tasks'] = active
        # A request goes first; while one is unfinished no sample is added.
        adopted = requests()
        waiting = next((t for t in adopted if not t.panda_task_id and not t.panda_tasks.exists()),
                       None)
        live = [t for t in adopted if (t.panda_task_id or t.pk) in active]
        if waiting is None and live:
            result.update(outcome='steady', reason='a production request runs; samples wait',
                          requests=[t.composed_name for t in live])
        elif waiting is None and len(active) >= 2:
            result.update(outcome='steady', reason='two finite production tasks already committed')
        else:
            from pcs import services
            from .front import full_output_rses, output_storage_problem
            from pcs.commands import build_evgen_task_params
            task = waiting or task or reserve_sample()
            problem = output_storage_problem(task.get_effective_config(), full_output_rses())
            if problem:
                raise RuntimeError(problem)
            es = task.overrides['event_service']
            jobs = es['events'] // es['events_per_job']
            spec = build_evgen_task_params(task)
            if spec['nJobs'] != jobs or spec['containerImage'] != cfg['native_image']:
                raise RuntimeError('Google task shape/image differs from its operating configuration')
            register_plan(task, cfg['proxy'])
            services.prodtask_submit_request(task=task, changed_by='wenaus',
                                             source='google_production', site=QUEUE)
            entry = task.overrides[MARKER]
            result.update(outcome='submitted',
                          reason=('submitted a production request' if entry.get('request')
                                  else 'replenished finite particle-gun work'),
                          task=task.composed_name, events=es['events'], jobs=jobs,
                          authority=entry['authority'])
    except Exception as exc:
        result['reason'] = f'{type(exc).__name__}: {exc}'
    get_product('google_production', lambda: result, ttl_seconds=900, refresh=True)
    log_epicprod_action('gke_pilots', 'google_production', username='wenaus',
                       outcome=result['outcome'], sublevel='normal',
                       live_default=result['outcome'] != 'steady',
                       message=f"Google production: {result['outcome']}: {result['reason']}",
                       **{k: v for k, v in result.items() if k not in ('outcome', 'reason')})
    return result
