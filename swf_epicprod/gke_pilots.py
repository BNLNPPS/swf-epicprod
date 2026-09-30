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
import hashlib
import logging
import os
import uuid

logger = logging.getLogger(__name__)

TEMPLATE_DEFAULT = os.path.join(os.path.dirname(__file__), 'gke', 'pilot-job.yaml')
PLACEHOLDER = '__SET_FROM_CLUSTER__'
WRAPPER = ('/cvmfs/eic.opensciencegrid.org/panda/pilot/canary/'
           'epicprod-pilot-wrapper.sh')
LABEL = 'epicprod-pilot'
DEFAULTS = {
    'enabled': False,
    'queue': 'BNL_ePIC_GOOGLE_es',
    'kubeconfig': '/etc/swf-monitor/gke-kubeconfig',
    'namespace': '',
    'template': '',
    'max_pods': 0,
    'cpu': '14',
    'memory': '100Gi',
    'getjobrequests': 5,
    'proxy': '/etc/swf-monitor/longproxy-for-rucio',
    'secret_name': 'epicprod-pilot-proxy',
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
    if not os.path.exists(cfg.get('kubeconfig') or ''):
        out.append(f"no kubeconfig at {cfg.get('kubeconfig')}")
    if template_text is not None and PLACEHOLDER in template_text:
        out.append('the pod template still has values to set from the cluster')
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
    container['resources'] = {'requests': dict(resources), 'limits': dict(resources)}
    queue = cfg['queue']
    container['command'] = ['/bin/bash', '-c']
    container['args'] = [
        f'exec {WRAPPER} -q {queue} -r {queue} -s {queue} -i PR -j managed '
        f"--getjobrequests {int(cfg['getjobrequests'])}"]
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
    from kubernetes import client, config
    api = config.new_client_from_config(config_file=cfg['kubeconfig'])
    return client.CoreV1Api(api), client.BatchV1Api(api)


def pod_supply(core, cfg):
    """Our pilot pods in the namespace by phase: (pending, running)."""
    pods = core.list_namespaced_pod(cfg['namespace'], label_selector=f'app={LABEL}').items
    pending = sum(1 for p in pods if p.status.phase == 'Pending')
    running = sum(1 for p in pods if p.status.phase == 'Running')
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


def run_cycle(*, dry_run=False, created_by='gke_pilots'):
    """One cycle. Returns the summary; records it when pods were started or
    the reason changed (never on a dry run)."""
    from monitor_app.epicprod_logging import log_epicprod_action
    cfg = settings()
    template_path = cfg['template'] or TEMPLATE_DEFAULT
    text, template = _load_template(template_path)
    summary = {'queue': cfg['queue'], 'started': 0, 'dry_run': dry_run}
    summary['activated'] = activated_jobs(cfg['queue'])
    stop = blockers(cfg, text)
    if stop:
        summary.update(outcome='held', reason='; '.join(stop))
    else:
        core, batch = _clients(cfg)
        pending, running = pod_supply(core, cfg)
        n, reason = decide(summary['activated'], pending, running, cfg['max_pods'])
        summary.update(pending=pending, running=running, to_start=n, reason=reason,
                       outcome='started' if n else 'steady')
        if not dry_run:
            summary['secret_written'] = ensure_proxy_secret(core, cfg)
            names = []
            for _ in range(n):
                job = render_job(template, cfg)
                batch.create_namespaced_job(cfg['namespace'], job)
                names.append(job['metadata']['name'])
            summary['started'] = len(names)
            summary['jobs'] = names
    if not dry_run and (summary['started'] or summary['reason'] != _last_reason()):
        log_epicprod_action(
            'gke_pilots', 'gke_pilot_cycle', username=created_by,
            outcome=summary['outcome'], sublevel='normal' if summary['started'] else 'low',
            live_default=bool(summary['started']),
            message=(f"{cfg['queue']}: {summary['outcome']} ({summary['reason']}); "
                     f"started {summary['started']}"),
            **{k: v for k, v in summary.items() if k not in ('jobs', 'outcome')})
    return summary
