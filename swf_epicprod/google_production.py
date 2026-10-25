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
EVENTS = 500000
MARKER = 'google_production'
MODULUS = 2147483647
AUTHORITY = '01a174ee-0d98-7656-a327-ddb85cde212d'


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
    from django.db import transaction
    from pcs.commands import internal_evgen_sample, prodtask_events_per_job
    from pcs.models import ProdTask
    from pcs.production_samples import compose
    with transaction.atomic():
        source = ProdTask.objects.select_for_update().get(pk=SOURCE)
        _, original_stem = internal_evgen_sample(source)
        chunks = -(-int(source.dataset.expected_events) // prodtask_events_per_job(source))
        reserved = seed_intervals(original_stem, chunks)
        samples = set()
        for task in ProdTask.objects.filter(overrides__has_key='event_service'):
            es = task.overrides['event_service']
            stem = es['row'].split(',')[0].rsplit('/', 1)[1]
            reserved.extend(seed_intervals(stem, int(es['events'])))
            samples.add(task.overrides.get('production_sample_prefix'))
        for index in range(1, 1000000):
            sample = f'gOP{index:06d}'
            stem = original_stem + '.' + sample
            seeds = seed_intervals(stem, EVENTS)
            if (sample not in samples and sum(b - a + 1 for a, b in seeds) == EVENTS
                    and not overlaps(seeds, reserved)):
                break
        else:
            raise RuntimeError('No disjoint Google production seed interval remains')
        es = dict(events=EVENTS, input_events=EVENTS, events_per_job=50000,
                  unit_events=250, close_s=300, deadline_s=7200, margin_s=300,
                  memory_mb_per_core=6000, slots='auto',
                  input_dataset='group.EIC:group.EIC.esplan.26.07.3.p1107.' + sample,
                  row=f'SINGLE/neutron/5GeV/3to50deg.{sample}/{stem},steer,{EVENTS},0000')
        task = compose(source, sample=sample, event_service=es, site=QUEUE,
                       created_by='wenaus')
        task.overrides[MARKER] = {'authority': AUTHORITY, 'sample': sample,
                                  'events': EVENTS, 'seed_stem': stem}
        task.save(update_fields=['overrides'])
        return task


def register_plan(task, proxy):
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
            _register_plan(task, str(private))
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _register_plan(task, proxy):
    os.environ.update(X509_USER_PROXY=proxy,
                      X509_CERT_DIR='/etc/grid-security/certificates')
    from django.conf import settings
    from rucio.client import Client
    from rucio.common.exception import DataIdentifierAlreadyExists, DataIdentifierNotFound
    from XRootD import client as xrd
    entry = task.overrides[MARKER]
    es = task.overrides['event_service']
    sample = entry['sample']
    plan = dict(schema='epicprod-particle-gun-generation-plan-v1', events=EVENTS,
                sample=sample, source_task=task.dataset.metadata['production_sample']['source'],
                generator='particle_gun', particle='neutron', gun_energy='5*GeV',
                theta_min_deg=3, theta_max_deg=50, detector_version='26.07.3',
                detector_config='epic_craterlake', detector_beams='5x41',
                seed_stem=entry['seed_stem'], authority=AUTHORITY,
                seed_scheme='(sha256(stem)[:7] * 10000 + zero_based_range_start + 1) modulo 2147483647; zero maps to one',
                event_number='zero-based absolute event position in this plan')
    data = (json.dumps(plan, indent=2) + '\n').encode()
    adler = f'{zlib.adler32(data) & 0xffffffff:08x}'
    lfn = 'group.EIC.gunplan.26.07.3.p1107.' + sample + '.json'
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
            'meta': {'guid': uuid.uuid4().hex, 'events': EVENTS}}])
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


def task_inventory():
    """Unknown, paused, failed or incomplete tasks hold replenishment."""
    from django.db import connections
    from pcs.models import ProdTask
    active, draft = [], None
    for task in ProdTask.objects.filter(overrides__has_key=MARKER).order_by('pk'):
        attempt = task.panda_tasks.order_by('-try_number').first()
        taskid = task.panda_task_id or (attempt.jedi_task_id if attempt else None)
        if taskid:
            with connections['panda'].cursor() as cursor:
                cursor.execute('SELECT status FROM doma_panda.jedi_tasks WHERE jeditaskid=%s', [taskid])
                row = cursor.fetchone()
            if not row:
                raise RuntimeError(f'No live PanDA state for Google task {taskid}')
            state = row[0]
            if state == 'done':
                continue
            if state in ('finished', 'failed', 'broken', 'aborted', 'exhausted', 'paused'):
                raise RuntimeError(f'Google task {taskid} is {state}; operator review required')
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
        if len(active) >= 2:
            result.update(outcome='steady', reason='two finite production tasks already committed')
        else:
            from pcs import services
            from .front import full_output_rses, output_storage_problem
            from pcs.commands import build_evgen_task_params
            task = task or reserve_sample()
            problem = output_storage_problem(task.get_effective_config(), full_output_rses())
            if problem:
                raise RuntimeError(problem)
            spec = build_evgen_task_params(task)
            if spec['nJobs'] != 10 or spec['containerImage'] != cfg['native_image']:
                raise RuntimeError('Google task shape/image differs from its operating configuration')
            register_plan(task, cfg['proxy'])
            services.prodtask_submit_request(task=task, changed_by='wenaus',
                                             source='google_production', site=QUEUE)
            result.update(outcome='submitted', reason='replenished finite particle-gun work',
                          task=task.composed_name, events=EVENTS, jobs=10,
                          authority=AUTHORITY)
    except Exception as exc:
        result['reason'] = f'{type(exc).__name__}: {exc}'
    get_product('google_production', lambda: result, ttl_seconds=900, refresh=True)
    log_epicprod_action('gke_pilots', 'google_production', username='wenaus',
                       outcome=result['outcome'], sublevel='normal',
                       live_default=result['outcome'] != 'steady',
                       message=f"Google production: {result['outcome']}: {result['reason']}",
                       **{k: v for k, v in result.items() if k not in ('outcome', 'reason')})
    return result
