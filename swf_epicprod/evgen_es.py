"""EVGEN input for Event Service production on Perlmutter
(docs/NERSC_PERLMUTTER.md, EVGEN input in production).

A PCS task over a registered EVGEN sample runs on a Perlmutter node through
the Event Service, its EVGEN files the job's input, in the shape validated on
the 5x130 DIS sample (tasks 41241, 41262): JEDI makes ranges over each file's
own events (``useRealNumEvents``), the payload simulates from the job's
staged copy (run.sh, EPICPROD_INPUT_LOCAL) and every unit records its source
file. Perlmutter takes no single-core work (Torre Wenaus, 2026-10-28: "NO
MORE SINGLE CORE 1/128 EFF WORK IN PERL EVER").

This module stages the input: the files of the sample the task's manifest
covers, copied from the payload's own JLab read path to BNL_PROD_DISK_1,
checked against JLab's adler32 and registered in the PanDA catalog with each
file's event count, in the task's input dataset. JEDI stops at the
manifest's total (``nEvents``), so the task processes what its manifest does.
"""
import os
import subprocess
import tempfile
import uuid
from collections import OrderedDict
from pathlib import Path

# The payload's EVGEN read path (payload/run.sh, XRDRURL and XRDRBASE).
JLAB_READ = 'root://dtn2304.jlab.org:8443//jlab-osdf-ro/eic/EPIC/volatile'
JLAB_SCOPE = 'epic'
BNL_RSE = 'BNL_PROD_DISK_1'
BNL_SCOPE = 'group.EIC'
LFN_PREFIX = 'group.EIC.esinput.'
STAGE_DIR = '/data/swf-tmp'
COPY_TIMEOUT_S = 3600


# The four-hour shape of the validated DIS run (task 41262: 5x130 DIS EVGEN,
# 128 slots, 100-event units, closes every 300 s, 32 shared reconstruction
# members, 1,500 MB a core; docs/NERSC_PERLMUTTER.md, Event Service jobs on
# Perlmutter) with the production settings the gun runs carry: ten-event
# ranges on the batch channel, concurrent closes, the job's log, the notice.
SHAPE = dict(unit_events=100, range_events=10, close_s=300, close_concurrency=4,
             deadline_s=14100, margin_s=600, memory_mb_per_core=1500, slots='128',
             pilot_cores=256, resident_sim=True, resident_unit=True, fast_merge=True,
             batch_channel=True, parallel_start=True, reco_pool=32, reco_async=True,
             report_barrier=True, notice=True, job_log=True,
             handoff_in_place=True, unit_local=True, input_names=True, local_scratch=True,
             nersc_input=True, local_work=True, record_per_close=True)


def capable(task, cfg=None):
    """True for a task this path can run: a registered EVGEN sample read as
    external EVGEN (not an internal generator), not yet submitted."""
    if task.panda_task_id is not None:
        return False
    if not ((task.overrides or {}).get('registered_sample') or {}).get('did'):
        return False
    cfg = cfg if cfg is not None else task.get_effective_config()
    return str((cfg.get('data') or {}).get('workflow_mode') or 'external_evgen') != 'internal_evgen'


class InputStaging(RuntimeError):
    """The task's input is being copied to its site; it starts when there."""


def prepare(task, proxy, site=''):
    """The task's input as its catalog holds it, staged at the site it is
    brokered to before it starts, and its ``event_service`` override set,
    saved: what a feed to a whole-node queue does before the submission
    request. Raises ``InputStaging`` while the site's copy is being made
    (the copy job is started, once, here); the front then holds the task,
    placed on the queue, and submits it at a later cycle."""
    staged = catalog_input(task)
    if SHAPE.get('nersc_input'):
        from .nersc_stage import ensure_staged
        done, detail = ensure_staged(task, staged, proxy)
        if not done:
            raise InputStaging(detail)
    task.overrides = dict(task.overrides or {}, event_service=event_service(task, staged))
    task.save(update_fields=['overrides'])
    return staged


def catalog_input(task):
    """The task's input where it resides, read from its catalog: the
    registered sample's dataset (the task's input dataset) and the files its
    manifest covers, each with its catalog name, size, adler32 and full
    event count, and the manifest's total. Nothing is copied or registered
    anywhere: ``{dataset, files: [{name, lfn, scope, bytes, adler32,
    events, used_events}], events}``, ``lfn`` the name's last component."""
    did = ((task.overrides or {}).get('registered_sample') or {}).get('did')
    if not did:
        raise ValueError(f'{task.composed_name}: no registered sample to read the input from')
    files, events = coverage(task)
    jlab = _jlab()
    out = []
    for path, entry in files.items():
        name = f'/EVGEN/{path}.{entry["ext"]}'
        meta = jlab.get_metadata(JLAB_SCOPE, name)
        if meta.get('events') in (None, ''):
            raise ValueError(f'{JLAB_SCOPE}:{name} carries no event count in its catalog')
        out.append({'name': name, 'lfn': os.path.basename(name), 'scope': JLAB_SCOPE,
                    'bytes': int(meta['bytes']), 'adler32': meta['adler32'],
                    'events': int(meta['events']), 'used_events': entry['events']})
    return {'dataset': did, 'files': out, 'events': events}


def event_service(task, staged):
    """The task's ``event_service`` override for its input: one job per file
    over the files' common event count and the task capped at its
    manifest's total, both in whole ranges (the batch pilot rebuilds a range
    from its first event and the range size). An input read from its
    catalog names its files (``input_files``), which JEDI takes from the
    dataset and no others."""
    k = SHAPE['range_events']
    per_file = min(f['events'] for f in staged['files']) // k * k
    names = [f['name'] for f in staged['files'] if f.get('name')]
    return dict(SHAPE, input='evgen', input_dataset=staged['dataset'], files=len(staged['files']),
                events_per_file=per_file, events=staged['events'] // k * k,
                **({'input_files': names} if names else {}))


def coverage(task):
    """The files the task's manifest covers, in manifest order:
    ``(OrderedDict {EVGEN-relative path: {ext, events}}, total events)``,
    the events being those the manifest's rows take from each file."""
    from pcs.commands import _evgen_manifest_from_inputs, prodtask_events_per_job
    per_job = prodtask_events_per_job(task)
    if per_job <= 0:
        raise ValueError(f'{task.composed_name}: no events per job to read its manifest')
    out = _evgen_manifest_from_inputs(task, per_job)
    rows = out[0] if isinstance(out, tuple) else out
    files = OrderedDict()
    for row in rows:
        path, ext, events = row.split(',')[:3]
        entry = files.setdefault(path, {'ext': ext, 'events': 0})
        if entry['ext'] != ext:
            raise ValueError(f'{path}: rows disagree on the extension')
        entry['events'] += int(events)
    if not files:
        raise ValueError(f'{task.composed_name}: the manifest covers no EVGEN file')
    return files, sum(f['events'] for f in files.values())


def input_dataset(task):
    """``group.EIC:group.EIC.esinput.<composed name past group.EIC.>``, the
    form of the validated canaries' input (group.EIC.esinput.26.07.3...)."""
    name = task.composed_name
    name = name[len(f'{BNL_SCOPE}.'):] if name.startswith(f'{BNL_SCOPE}.') else name
    return f'{BNL_SCOPE}:{LFN_PREFIX}{name}'


def lfn(path, ext):
    """The staged copy's name: the source file's own name, so a job's
    outputs are named from its input (payload/es/es_slot.py)."""
    return f'{LFN_PREFIX}{os.path.basename(path)}.{ext}'


def stage(task, proxy):
    """Copy and register the task's input; idempotent. Returns
    ``{dataset, files: [{lfn, events, bytes}], events}``."""
    keys = ('X509_USER_PROXY', 'X509_CERT_DIR')
    previous = {key: os.environ.get(key) for key in keys}
    try:
        # GSI refuses the group-readable service proxy: a private copy for
        # this staging only, as the gun feeder's plan registration does.
        with tempfile.TemporaryDirectory(prefix='evgen-es-proxy-', dir=STAGE_DIR) as temp:
            private = Path(temp) / 'proxy'
            private.write_bytes(Path(proxy).read_bytes())
            private.chmod(0o600)
            os.environ.update(X509_USER_PROXY=str(private),
                              X509_CERT_DIR='/etc/grid-security/certificates')
            return _stage(task, str(private))
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _jlab():
    """JLab's catalog, read-only, as the JLab Rucio MCP tools read it."""
    from django.conf import settings
    from rucio.client import Client
    return Client(rucio_host=settings.RUCIO_JLAB_URL, auth_host=settings.RUCIO_JLAB_URL,
                  account=settings.RUCIO_JLAB_ACCOUNT, auth_type='userpass',
                  creds={'username': settings.RUCIO_JLAB_USERNAME,
                         'password': settings.RUCIO_JLAB_PASSWORD},
                  ca_cert=False, timeout=40)


def _catalogs(proxy):
    from django.conf import settings
    from rucio.client import Client
    bnl = Client(rucio_host=settings.RUCIO_BNL_URL, auth_host=settings.RUCIO_BNL_URL,
                 account=settings.RUCIO_BNL_ACCOUNT, vo=settings.RUCIO_BNL_VO,
                 auth_type='x509_proxy', creds={'client_proxy': proxy}, ca_cert=None, timeout=40)
    # JLab's catalog, read-only, as the JLab Rucio MCP tools read it.
    jlab = Client(rucio_host=settings.RUCIO_JLAB_URL, auth_host=settings.RUCIO_JLAB_URL,
                  account=settings.RUCIO_JLAB_ACCOUNT, auth_type='userpass',
                  creds={'username': settings.RUCIO_JLAB_USERNAME,
                         'password': settings.RUCIO_JLAB_PASSWORD},
                  ca_cert=False, timeout=40)
    return bnl, jlab


def _stage(task, proxy):
    from rucio.common.exception import DataIdentifierAlreadyExists, DataIdentifierNotFound
    files, events = coverage(task)
    bnl, jlab = _catalogs(proxy)
    env = dict(os.environ, X509_USER_PROXY=proxy, X509_CERT_DIR='/etc/grid-security/certificates')
    staged = []
    for path, entry in files.items():
        source = jlab.get_metadata(JLAB_SCOPE, f'/EVGEN/{path}.{entry["ext"]}')
        name = lfn(path, entry['ext'])
        meta = {'bytes': int(source['bytes']), 'adler32': source['adler32'],
                'events': int(source['events'])}
        try:
            have = bnl.get_metadata(BNL_SCOPE, name)
            if int(have['bytes']) != meta['bytes'] or have['adler32'] != meta['adler32']:
                raise RuntimeError(f'{name} differs in the BNL catalog from its JLab source')
        except DataIdentifierNotFound:
            pfn = bnl.lfns2pfns(BNL_RSE, [f'{BNL_SCOPE}:{name}'], operation='write',
                                scheme='root')[f'{BNL_SCOPE}:{name}']
            with tempfile.TemporaryDirectory(prefix='evgen-es-', dir=STAGE_DIR) as temp:
                local = os.path.join(temp, name)
                check = f'adler32:{meta["adler32"]}'
                subprocess.run(['xrdcp', '--cksum', check, f'{JLAB_READ}/EVGEN/{path}.{entry["ext"]}',
                                local], check=True, timeout=COPY_TIMEOUT_S, env=env)
                subprocess.run(['xrdcp', '--cksum', check, local, pfn], check=True,
                               timeout=COPY_TIMEOUT_S, env=env)
            bnl.add_replicas(BNL_RSE, [{'scope': BNL_SCOPE, 'name': name, 'bytes': meta['bytes'],
                                        'adler32': meta['adler32'],
                                        'meta': {'guid': uuid.uuid4().hex, 'events': meta['events']}}])
        staged.append({'lfn': name, 'events': meta['events'], 'bytes': meta['bytes'],
                       'adler32': meta['adler32'], 'used_events': entry['events']})
    scope, dataset = input_dataset(task).split(':', 1)
    try:
        bnl.add_dataset(scope, dataset)
    except DataIdentifierAlreadyExists:
        pass
    attached = {f['name'] for f in bnl.list_files(scope, dataset)}
    missing = [f['lfn'] for f in staged if f['lfn'] not in attached]
    extra = attached - {f['lfn'] for f in staged}
    if extra:
        raise RuntimeError(f'{dataset} holds files the manifest does not cover: {sorted(extra)}')
    if missing:
        bnl.attach_dids(scope, dataset, [{'scope': BNL_SCOPE, 'name': n} for n in missing])
    if not list(bnl.list_did_rules(scope, dataset)):
        bnl.add_replication_rule([{'scope': scope, 'name': dataset}], 1, BNL_RSE)
    if bnl.get_metadata(scope, dataset).get('open'):
        bnl.close(scope, dataset)
    return {'dataset': input_dataset(task), 'files': staged, 'events': events}
