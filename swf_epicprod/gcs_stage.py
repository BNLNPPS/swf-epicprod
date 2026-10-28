"""A task's EVGEN input staged in the GKE cluster's Cloud Storage bucket
before the task starts (docs/GKE_PILOT_FLOW.md, EVGEN input): the Google
counterpart of ``nersc_stage``.

When the front brokers a task to Google, its whole input, the files its
manifest covers, is copied into the bucket from the replicas its catalog
lists, and the task is held until the copy is done. Each job's pilot then
fetches its own file in-region into the pod (batch pilot ``es_batch``,
``EPICPROD_SITE_INPUT_GCS``). The copy is a Kubernetes Job in the cluster:
the nodes' service account writes the bucket, which the agent's own account
cannot. It reads each file with ``xrdcp`` from ATLAS's tools on CVMFS and the
production proxy the pilots hold (the Secret ``gke_pilots.secret_name``),
checks it against the catalog's adler32, uploads it and reads its size back;
a file already in the bucket at its catalog size is left as it is. The job's
success is the record that every file of the task is in the bucket.

The bucket and its prefix are ``gke_pilots.site_input_gcs`` (``bucket/prefix``).
"""
import json
import shlex
import time
import urllib.parse

from .nersc_stage import SETUP, catalog_urls

IMAGE = 'atlasadc/atlas-grid-almalinux9'
JOB_LABEL = 'epicprod-stage'
DEADLINE_S = 3 * 3600
KEEP_S = 7 * 86400
TOKEN = ('curl -s -H "Metadata-Flavor: Google" '
         'http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token'
         ' | python3 -c \'import sys, json; print(json.load(sys.stdin)["access_token"])\'')


def location(cfg=None):
    """``(bucket, prefix)`` of the site's staged inputs; raises without one."""
    if cfg is None:
        from .gke_pilots import settings
        cfg = settings()
    where = str(cfg.get('site_input_gcs') or '').strip('/')
    if not where:
        raise RuntimeError('gke_pilots.site_input_gcs names no bucket for Google\'s staged inputs')
    bucket, _, prefix = where.partition('/')
    return bucket, prefix


def object_name(prefix, lfn):
    return f'{prefix}/{lfn}' if prefix else lfn


def copy_script(files, sources, bucket, prefix):
    """The copy job's script: each file not in the bucket at its catalog
    size copied from its catalog replica, checked against its adler32,
    uploaded and read back at its size. Exits non-zero on any failure."""
    q = shlex.quote
    lines = [SETUP, 'set -u', 'install -m 600 /proxy/x509up /work/x509up',
             'export X509_USER_PROXY=/work/x509up', 'failed=0',
             f'token() {{ {TOKEN}; }}',
             'size_of() { curl -s -H "Authorization: Bearer $(token)" '
             f'"https://storage.googleapis.com/storage/v1/b/{bucket}/o/$1?fields=size" '
             '| python3 -c \'import sys, json; print(json.load(sys.stdin).get("size", ""))\'; }']
    for f in files:
        obj = urllib.parse.quote(object_name(prefix, f['lfn']), safe='')
        local = f'/work/{f["lfn"]}'
        upload = (f'https://storage.googleapis.com/upload/storage/v1/b/{bucket}/o'
                  f'?uploadType=media&name={obj}')
        lines.append(
            f'if [ "$(size_of {q(obj)})" = "{int(f["bytes"])}" ]; then echo "present {f["lfn"]}"; '
            f'elif xrdcp --nopbar --force --cksum {q("adler32:" + f["adler32"])} {q(sources[f["lfn"]])} {q(local)}'
            f' && curl -sS --fail -o /dev/null -X POST -T {q(local)} -H "Authorization: Bearer $(token)"'
            f' -H "Content-Type: application/octet-stream" {q(upload)}'
            f' && [ "$(size_of {q(obj)})" = "{int(f["bytes"])}" ]; then echo "ok {f["lfn"]}"; '
            f'else echo "FAILED {f["lfn"]}"; failed=1; fi; rm -f {q(local)}')
    lines.append('exit $failed')
    return '\n'.join(lines)


def bucket_script(bucket, location_name='US-EAST4'):
    """A script that makes the bucket in the cluster's project when it is
    absent, uniform access, standard storage, in the cluster's region."""
    body = json.dumps({'name': bucket, 'location': location_name, 'storageClass': 'STANDARD',
                       'iamConfiguration': {'uniformBucketLevelAccess': {'enabled': True}}})
    return '\n'.join([
        'set -u', f'token() {{ {TOKEN}; }}',
        'P=$(curl -s -H "Metadata-Flavor: Google" '
        'http://metadata.google.internal/computeMetadata/v1/project/project-id)',
        f'code=$(curl -s -o /dev/null -w "%{{http_code}}" -H "Authorization: Bearer $(token)" '
        f'"https://storage.googleapis.com/storage/v1/b/{bucket}")',
        f'if [ "$code" = 200 ]; then echo "present {bucket}"; exit 0; fi',
        f'curl -sS --fail -o /dev/null -X POST -H "Authorization: Bearer $(token)" -H "Content-Type: application/json"'
        f' "https://storage.googleapis.com/storage/v1/b?project=$P" -d {shlex.quote(body)}'
        f' && echo "made {bucket} in $P" || {{ echo "FAILED {bucket}"; exit 1; }}'])


def job_manifest(name, script, cfg):
    """A Kubernetes Job running ``script`` on the pilots' node pool, with
    CVMFS and the pilots' proxy Secret."""
    return {
        'apiVersion': 'batch/v1', 'kind': 'Job',
        'metadata': {'name': name, 'labels': {'app': JOB_LABEL}},
        'spec': {
            'backoffLimit': 2, 'activeDeadlineSeconds': DEADLINE_S, 'ttlSecondsAfterFinished': KEEP_S,
            'template': {
                'metadata': {'labels': {'app': JOB_LABEL}},
                'spec': {
                    'restartPolicy': 'Never',
                    'nodeSelector': {'cloud.google.com/gke-nodepool': 'epic-main-16-20gb'},
                    'tolerations': [{'key': 'cloud.google.com/gke-spot', 'operator': 'Equal',
                                     'value': 'true', 'effect': 'NoSchedule'}],
                    'containers': [{
                        'name': 'stage', 'image': IMAGE, 'command': ['bash', '-c', script],
                        'resources': {'requests': {'cpu': '200m', 'memory': '1Gi'},
                                      'limits': {'cpu': '2', 'memory': '4Gi'}},
                        'volumeMounts': [
                            {'name': 'cvmfs', 'mountPath': '/cvmfs', 'mountPropagation': 'HostToContainer',
                             'readOnly': True},
                            {'name': 'proxy', 'mountPath': '/proxy', 'readOnly': True},
                            {'name': 'work', 'mountPath': '/work'}]}],
                    'volumes': [
                        {'name': 'cvmfs', 'hostPath': {'path': '/var/lib/cvmfs-k8s', 'type': 'Directory'}},
                        {'name': 'proxy', 'secret': {'secretName': cfg['secret_name'], 'defaultMode': 0o440}},
                        {'name': 'work', 'emptyDir': {}}]}}}}


def submit(name, script, cfg=None, clients=None):
    from .gke_pilots import _clients, settings
    cfg = cfg or settings()
    _, batch = clients or _clients(cfg)
    batch.create_namespaced_job(cfg['namespace'], job_manifest(name, script, cfg))
    return name


def job_state(name, cfg=None, clients=None):
    """``succeeded``, ``failed``, ``running`` or ``missing``."""
    from kubernetes.client.exceptions import ApiException
    from .gke_pilots import _clients, settings
    cfg = cfg or settings()
    _, batch = clients or _clients(cfg)
    try:
        status = batch.read_namespaced_job_status(name, cfg['namespace']).status
    except ApiException as exc:
        if exc.status == 404:
            return 'missing'
        raise
    if status.succeeded:
        return 'succeeded'
    if status.failed and not status.active:
        return 'failed'
    return 'running'


def job_log(name, cfg=None, clients=None):
    from .gke_pilots import _clients, settings
    cfg = cfg or settings()
    core, _ = clients or _clients(cfg)
    pods = core.list_namespaced_pod(cfg['namespace'], label_selector=f'job-name={name}').items
    return '\n'.join(core.read_namespaced_pod_log(p.metadata.name, cfg['namespace']) for p in pods)


def ensure_staged(task, staged, proxy=None, cfg=None):
    """``(True, detail)`` once a copy job of the task's whole input
    (``evgen_es.catalog_input``) has succeeded; else one is under way,
    started here if none is, and ``(False, detail)``. The job is the task's
    ``overrides['site_input']``. ``proxy`` is unused: the job reads with the
    pilots' proxy Secret, which the pilot cycle keeps current."""
    from .gke_pilots import _clients, settings
    cfg = cfg or settings()
    bucket, prefix = location(cfg)
    clients = _clients(cfg)
    overrides = dict(task.overrides or {})
    record = overrides.get('site_input') or {}
    names = sorted(f['lfn'] for f in staged['files'])
    if record.get('site') == 'gcs' and record.get('lfns') == names and record.get('job'):
        state = job_state(record['job'], cfg, clients)
        if state == 'succeeded':
            return True, f'all {len(names)} input files in gs://{bucket}/{prefix} (job {record["job"]})'
        if state == 'running':
            return False, f'{len(names)} input files being copied to gs://{bucket}/{prefix} (job {record["job"]})'
    sample = next((p for p in task.composed_name.split('.') if p[:1] == 'p' and p[1:].isdigit()), 'task')
    name = f'{JOB_LABEL}-{sample}-' + time.strftime('%Y%m%d%H%M%S')
    submit(name, copy_script(staged['files'], catalog_urls(staged['files']), bucket, prefix), cfg, clients)
    overrides['site_input'] = {'site': 'gcs', 'job': name, 'lfns': names, 'files': len(names),
                               'submitted_at': time.time()}
    task.overrides = overrides
    task.save(update_fields=['overrides'])
    return False, f'{len(names)} input files being copied to gs://{bucket}/{prefix} (job {name})'
