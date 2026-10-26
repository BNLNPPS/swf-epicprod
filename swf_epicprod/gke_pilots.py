"""The pilot flow of BNL_ePIC_GOOGLE_es (docs/GKE_PILOT_FLOW.md): the
production operations agent starts the queue's pilot pods in the GKE
cluster, one Kubernetes Job per pilot, in step with the queue's
activated jobs and under a cap.

``decide`` is pure. ``run_cycle`` reads the backlog from PanDA and the
supply from the cluster, keeps the proxy Secret current, starts the
pods ``decide`` asks for, and records the cycle when it starts pods or
its reason changes. It holds the cluster credential (a kubeconfig on
the agent's host) and starts nothing unless every setting it needs is
in place.
"""
import copy
import fcntl
import hashlib
import logging
import os
import shlex
import uuid

logger = logging.getLogger(__name__)

TEMPLATE_DEFAULT = os.path.join(os.path.dirname(__file__), 'gke', 'pilot-job.yaml')
PLACEHOLDER = '__SET_FROM_CLUSTER__'
NATIVE_RUNTIME = ('/cvmfs/eic.opensciencegrid.org/panda/pilot/canary/'
                  'native-runtime-e8cc102')
LABEL = 'epicprod-pilot'
CAPABILITY_KEY = 'gke_pilot_runtime'
DEFAULTS = {
    'enabled': False,
    'queue': 'BNL_ePIC_GOOGLE_es',
    # A Google service-account key; the cluster's endpoint and CA are read
    # from the Kubernetes Engine API with it at each cycle.
    'credential': '/etc/swf-monitor/gke-sa.json',
    'cluster': 'epic-panda-us-east4',
    'location': 'us-east4',
    'namespace': 'default',
    'template': '',
    'max_pods': 0,
    'cpu': '14',
    'cpu_limit': '15.8',
    'memory': '100Gi',
    'getjobrequests': 5,
    'proxy': '/etc/swf-monitor/longproxy-for-rucio',
    'secret_name': 'epicprod-pilot-proxy',
    'native_runtime': NATIVE_RUNTIME,
    'native_image': '/cvmfs/singularity.opensciencegrid.org/eicweb/eic_xl:26.07.3-stable',
    'es_channel': ('/cvmfs/eic.opensciencegrid.org/panda/pilot/canary/'
                   'es-channel-py313-debian13'),
    'queuedata': ('/cvmfs/eic.opensciencegrid.org/panda/pilot/canary/'
                  'queuedata/BNL_ePIC_GOOGLE_es.json'),
}


def settings():
    from monitor_app.models import SysConfig
    return {k: SysConfig.get_setting(f'gke_pilots.{k}', v) for k, v in DEFAULTS.items()}


def decide(activated, pending, running, max_pods):
    """How many pilot pods to start. Pure.

    ``activated``: the queue's jobs waiting for a pilot. ``pending``: our
    pods not yet running (each will take one of those jobs); ``running``:
    our pods at work. Start one pod per activated job not already covered
    by a waiting pod, never taking the pods alive past ``max_pods``.
    Returns (count, reason)."""
    activated, pending, running = int(activated), int(pending), int(running)
    cap = int(max_pods or 0)
    if cap <= 0:
        return 0, 'max_pods is 0'
    uncovered = activated - pending
    if uncovered <= 0:
        return 0, ('no activated jobs' if activated == 0
                   else f'{pending} pods waiting cover {activated} activated jobs')
    room = cap - pending - running
    if room <= 0:
        return 0, f'at the cap: {pending + running} pods alive of {cap}'
    n = min(uncovered, room)
    return n, f'{activated} activated, {pending} pods waiting, {running} working, cap {cap}'


def blockers(cfg, template_text=None):
    """What stops the cycle from starting pods, as reasons. Pure."""
    out = []
    if not cfg.get('enabled'):
        out.append('gke_pilots.enabled is false')
    if not cfg.get('namespace'):
        out.append('gke_pilots.namespace is empty')
    if int(cfg.get('max_pods') or 0) <= 0:
        out.append('gke_pilots.max_pods is 0')
    if not os.path.exists(cfg.get('credential') or ''):
        out.append(f"no cluster credential at {cfg.get('credential')}")
    if template_text is not None and PLACEHOLDER in template_text:
        out.append('the pod template still has values to set from the cluster')
    for key, filename in (('native_runtime', 'epic-native-pilot.sh'),
                          ('native_runtime', 'pilot3-native.tar.gz')):
        if not os.path.isfile(os.path.join(cfg.get(key) or '', filename)):
            out.append(f"native runtime unavailable: {key}/{filename}")
    if not os.path.isdir(os.path.join(cfg.get('es_channel') or '', 'python')):
        out.append('Python-matched ES channel unavailable')
    return out


def render_job(template, cfg, name=None):
    """The Job for one pilot: the template with the cycle's name, labels,
    resources, proxy Secret and command set. Pure."""
    job = copy.deepcopy(template)
    name = name or f'{LABEL}-{uuid.uuid4().hex[:10]}'
    labels = {'app': LABEL, 'panda-queue': cfg['queue'].lower().replace('_', '-')}
    job.setdefault('metadata', {})
    job['metadata']['name'] = name
    job['metadata']['labels'] = dict(job['metadata'].get('labels') or {}, **labels)
    pod = job['spec']['template']
    pod.setdefault('metadata', {})
    pod['metadata']['labels'] = dict(pod['metadata'].get('labels') or {}, **labels)
    spec = pod['spec']
    for vol in spec.get('volumes') or []:
        if 'secret' in vol:
            vol['secret']['secretName'] = cfg['secret_name']
    container = spec['containers'][0]
    resources = {'cpu': str(cfg['cpu']), 'memory': str(cfg['memory'])}
    # The request sizes the pod to the node for scheduling; a higher CPU
    # limit lets the shared reconstruction use what the node's system pods
    # leave idle (GKE_PILOT_FLOW.md, Settings).
    limits = dict(resources, cpu=str(cfg.get('cpu_limit') or cfg['cpu']))
    container['resources'] = {'requests': dict(resources), 'limits': limits}
    queue = cfg['queue']
    runtime = cfg['native_runtime']
    # The same native archive/launcher as production; site settings supply
    # the image, credentials and ES channel. No resource-type filter: the
    # queue can generate SCORE jobs for a whole-node ES harness.
    exports = {
        'EPICPROD_NATIVE_IMAGE': cfg['native_image'],
        'EPICPROD_NATIVE_ARCHIVE': 'pilot3-native.tar.gz',
        'EPICPROD_NATIVE_BATCH': '1',
        'EPICPROD_NATIVE_NESTING': 'off',
        'EPICPROD_NATIVE_PROXY': 'x509up',
        'EPICPROD_ES_CHANNEL': cfg['es_channel'],
        'PILOT_ES_EXECUTOR_TYPE': 'generic',
        'ATHENA_PROC_NUMBER': str(int(cfg['cpu'])),
    }
    # A runtime whose manifest declares the bounded ES batch channel gets its
    # gate: that pilot fetches 256 ranges per server call and refuses ES
    # stage-out without it (NERSC_PERLMUTTER.md, Bounded ES batch-channel
    # correction). A legacy-channel payload still gets one range per request.
    batch = cfg.get('es_batch_channel')
    if batch:
        exports.update(EPICPROD_SWITCH_ES_BATCH_CHANNEL='1',
                       EPICPROD_ES_STAGEOUT_GAP=str(int(batch['stageout_s'])))
    command = ['bash', '/pilotdir/epic-native-pilot.sh', '-q', queue,
               '-i', 'PR', '-j', 'managed', '-t', '-w', 'generic',
               '--pilot-user', 'epic', '--url', 'https://pandaserver01.sdcc.bnl.gov',
               '-p', '25443', '--allow-same-user=False', '--job-type=managed',
               '--use-rucio-traces', 'False', '--rucio-host', 'https://nprucio01.sdcc.bnl.gov:443',
               '--queuedata-url', 'file://' + cfg['queuedata'],
               '--storagedata-url', 'https://datalake-cric.cern.ch/cache/ddmendpoints.json',
               '--getjobrequests', str(int(cfg['getjobrequests'])), '--noworkerpilotstatusupdate']
    container['command'] = ['/bin/bash', '-c']
    # The proxy Secret is mounted read-only and group-readable; grid tools
    # want their own copy at mode 600, as Harvester's pods make it.
    container['args'] = [
        'install -m 600 /proxy/x509up /pilotdir/x509up && '
        'cp -R ' + shlex.quote(runtime) + '/. /pilotdir/ && '
        'export ' + ' '.join(k + '=' + shlex.quote(v) for k, v in exports.items()) + ' && '
        'exec ' + ' '.join(shlex.quote(v) for v in command)]
    return job


def activated_jobs(queue):
    """The queue's jobs waiting for a pilot (PanDA jobsactive4)."""
    from django.db import connections
    from monitor_app.panda.constants import PANDA_SCHEMA
    with connections['panda'].cursor() as cur:
        cur.execute(f'SELECT COUNT(*) FROM "{PANDA_SCHEMA}"."jobsactive4" '
                    'WHERE "computingsite" = %s AND "jobstatus" = %s',
                    [queue, 'activated'])
        return int(cur.fetchone()[0])


def _clients(cfg):
    """Kubernetes clients for the cluster, authenticated as the service
    account: a short-lived token from its key, the endpoint and CA from
    the Kubernetes Engine API."""
    import base64
    import json
    import tempfile
    import requests
    from google.oauth2 import service_account
    import google.auth.transport.requests as gtr
    from kubernetes import client
    creds = service_account.Credentials.from_service_account_file(
        cfg['credential'], scopes=['https://www.googleapis.com/auth/cloud-platform'])
    creds.refresh(gtr.Request())
    with open(cfg['credential']) as f:
        project = json.load(f)['project_id']
    r = requests.get(
        f"https://container.googleapis.com/v1/projects/{project}/locations/"
        f"{cfg['location']}/clusters/{cfg['cluster']}",
        headers={'Authorization': f'Bearer {creds.token}'}, timeout=30)
    r.raise_for_status()
    cluster = r.json()
    ca = tempfile.NamedTemporaryFile(prefix='gke-ca-', suffix='.crt', delete=False)
    ca.write(base64.b64decode(cluster['masterAuth']['clusterCaCertificate']))
    ca.close()
    conf = client.Configuration()
    conf.host = 'https://' + cluster['endpoint']
    conf.ssl_ca_cert = ca.name
    conf.api_key = {'authorization': 'Bearer ' + creds.token}
    api = client.ApiClient(conf)
    return client.CoreV1Api(api), client.BatchV1Api(api)


def pod_supply(core, batch, cfg, observation=None):
    """Count live Jobs even before Kubernetes has created their pods."""
    pods = core.list_namespaced_pod(cfg['namespace'], label_selector=f'app={LABEL}').items
    jobs = batch.list_namespaced_job(cfg['namespace'], label_selector=f'app={LABEL}').items
    live = {j.metadata.name for j in jobs
            if not any(c.status == 'True' and c.type in ('Complete', 'Failed')
                       for c in (j.status.conditions or []))}
    working = set()
    for pod in pods:
        if pod.status.phase not in ('Pending', 'Running', 'Unknown'):
            continue
        owner = next((o.name for o in (pod.metadata.owner_references or []) if o.kind == 'Job'),
                     pod.metadata.name)
        live.add(owner)  # Also count a terminating pod after its Job was deleted.
        if pod.status.phase == 'Running':
            working.add(owner)
    running = len(working)
    pending = len(live) - running
    if observation is not None:
        records = []
        failed = 0
        for job in jobs:
            conditions = job.status.conditions or []
            ended = next((c for c in conditions if c.status == 'True'
                          and c.type in ('Complete', 'Failed')), None)
            phase = ('Failed' if ended.type == 'Failed' else 'Succeeded') if ended else 'Pending'
            failed += phase == 'Failed'
            at = getattr(job.metadata, 'creation_timestamp', None)
            records.append({'name': job.metadata.name, 'phase': phase,
                            'at': at.isoformat() if at else '',
                            'failed': phase == 'Failed',
                            'reason': getattr(ended, 'reason', None) or phase,
                            'detail': str(getattr(ended, 'message', None) or '')[:400]})
        for pod in pods:
            at = getattr(pod.metadata, 'creation_timestamp', None)
            state = {'name': pod.metadata.name, 'phase': pod.status.phase,
                     'at': at.isoformat() if at else '',
                     'failed': pod.status.phase == 'Failed',
                     'reason': getattr(pod.status, 'reason', None) or pod.status.phase,
                     'detail': str(getattr(pod.status, 'message', None) or '')[:400]}
            statuses = (getattr(pod.status, 'init_container_statuses', None) or []) + (
                getattr(pod.status, 'container_statuses', None) or [])
            for container in statuses:
                terminated = getattr(container.state, 'terminated', None)
                waiting = getattr(container.state, 'waiting', None)
                problem = (terminated if terminated and terminated.exit_code else
                           waiting if waiting and waiting.reason not in
                           ('ContainerCreating', 'PodInitializing') else None)
                if problem:
                    state.update(failed=True, reason=problem.reason,
                                 detail=str(problem.message or '')[:400])
                    break
            records.append(state)
        observation.update(pending=pending, running=running,
                           failed_retained=failed,
                           latest=max(records, key=lambda r: r['at'], default=None))
    return pending, running


def ensure_proxy_secret(core, cfg):
    """Keep the proxy Secret equal to the proxy file; returns True when it
    was written."""
    import base64
    from kubernetes.client.rest import ApiException
    with open(cfg['proxy'], 'rb') as f:
        data = f.read()
    digest = hashlib.sha256(data).hexdigest()[:16]
    body = {'metadata': {'name': cfg['secret_name'],
                         'labels': {'app': LABEL},
                         'annotations': {'epicprod/sha256': digest}},
            'type': 'Opaque',
            'data': {'x509up': base64.b64encode(data).decode()}}
    try:
        current = core.read_namespaced_secret(cfg['secret_name'], cfg['namespace'])
    except ApiException as exc:
        if exc.status != 404:
            raise
        core.create_namespaced_secret(cfg['namespace'], body)
        return True
    if ((current.metadata.annotations or {}).get('epicprod/sha256')) == digest:
        return False
    core.replace_namespaced_secret(cfg['secret_name'], cfg['namespace'], body)
    return True


def _load_template(path):
    import yaml
    with open(path) as f:
        text = f.read()
    return text, yaml.safe_load(text)


def _last_reason():
    from monitor_app.models import AppLog
    row = (AppLog.objects.filter(app_name='epicprod', extra_data__action='gke_pilot_cycle')
           .order_by('-timestamp').values('extra_data').first())
    return ((row or {}).get('extra_data') or {}).get('reason')


def runtime_manifest(cfg):
    """The published manifest of the configured native runtime."""
    import json
    with open(os.path.join(cfg['native_runtime'], 'manifest.json')) as handle:
        return json.load(handle)


def publish_capability(cfg, summary, template_text):
    """Admission evidence from the controller's checked runtime and pod template."""
    import json
    from monitor_app.cached_product import get_product
    cap = {'queue': cfg['queue'], 'mode': 'unknown', 'images': [],
           'source': cfg['native_runtime'], 'max_pods': int(cfg['max_pods']),
           'enabled': bool(cfg['enabled']), 'cpus_per_worker': int(cfg['cpu']),
           'outcome': summary['outcome'], 'reason': summary['reason'],
           'workers': summary.get('workers'),
           'template_sha256': hashlib.sha256(template_text.encode()).hexdigest()}
    if summary['outcome'] == 'held':
        cap['error'] = summary['reason']
    else:
        path = os.path.join(cfg['native_runtime'], 'manifest.json')
        with open(path) as handle:
            manifest = json.load(handle)
        archive = os.path.join(cfg['native_runtime'], 'pilot3-native.tar.gz')
        with open(archive, 'rb') as handle:
            digest = hashlib.sha256(handle.read()).hexdigest()
        if digest != manifest['pilot_archive_sha256']:
            raise ValueError('native archive differs from its published manifest')
        cap.update(mode='bound', images=[cfg['native_image']],
                   pilot_archive_sha256=digest, source_commit=manifest['source_commit'])
    get_product(CAPABILITY_KEY, lambda: cap, ttl_seconds=900, refresh=True)


def run_cycle(*, dry_run=False, created_by='gke_pilots'):
    """Serialize every local controller invocation before measuring supply."""
    root = os.environ.get('SWF_TMP_DIR', '/data/swf-tmp')
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, 'gke-pilot-cycle.lock'), 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'queue': 'BNL_ePIC_GOOGLE_es', 'activated': 0, 'started': 0,
                    'dry_run': dry_run, 'outcome': 'held',
                    'reason': 'another GKE pilot cycle is active'}
        return _run_cycle(dry_run=dry_run, created_by=created_by)


def _run_cycle(*, dry_run=False, created_by='gke_pilots'):
    """One cycle. Returns the summary; records it when pods were started or
    the reason changed (never on a dry run)."""
    from monitor_app.epicprod_logging import log_epicprod_action
    cfg = settings()
    template_path = cfg['template'] or TEMPLATE_DEFAULT
    text, template = _load_template(template_path)
    summary = {'queue': cfg['queue'], 'started': 0, 'dry_run': dry_run}
    summary['activated'] = activated_jobs(cfg['queue'])
    # The placeholder is looked for in the template's values, not its
    # comments, which name it.
    import yaml
    stop = blockers(cfg, yaml.safe_dump(template))
    from swf_epicprod.front import closed_queues
    closed = closed_queues()
    if cfg['queue'] in closed:
        stop.append(f"queue closed: {closed[cfg['queue']]}")
    if stop:
        summary.update(outcome='held', reason='; '.join(stop))
    else:
        core, batch = _clients(cfg)
        workers = {}
        pending, running = pod_supply(core, batch, cfg, observation=workers)
        summary['workers'] = workers
        n, reason = decide(summary['activated'], pending, running, cfg['max_pods'])
        summary.update(pending=pending, running=running, to_start=n, reason=reason,
                       outcome='started' if n else 'steady')
        if not dry_run:
            summary['secret_written'] = ensure_proxy_secret(core, cfg)
            cfg['es_batch_channel'] = runtime_manifest(cfg).get('es_batch_channel')
            names = []
            for _ in range(n):
                job = render_job(template, cfg)
                batch.create_namespaced_job(cfg['namespace'], job)
                names.append(job['metadata']['name'])
            summary['started'] = len(names)
            summary['jobs'] = names
            workers['pending'] += len(names)
            if names:
                from datetime import datetime, timezone
                workers['latest'] = {'name': names[-1], 'phase': 'Pending',
                                     'at': datetime.now(timezone.utc).isoformat(),
                                     'failed': False, 'reason': 'Job created',
                                     'detail': 'Waiting for the pilot pod'}
    if not dry_run and (summary['started'] or summary['reason'] != _last_reason()):
        log_epicprod_action(
            'gke_pilots', 'gke_pilot_cycle', username=created_by,
            outcome=summary['outcome'], sublevel='normal' if summary['started'] else 'low',
            live_default=bool(summary['started']),
            message=(f"{cfg['queue']}: {summary['outcome']} ({summary['reason']}); "
                     f"started {summary['started']}"),
            **{k: v for k, v in summary.items() if k not in ('jobs', 'outcome')})
    if not dry_run:
        publish_capability(cfg, summary, text)
        if summary['outcome'] != 'held':
            from .google_production import replenish
            summary['production'] = replenish(cfg)
    return summary
