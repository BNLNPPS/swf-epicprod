"""The NERSC IRI API client: one for every production reader and writer
(the allocation balance, swf_epicprod/nersc_allocation.py; the EVGEN input
copy at NERSC, swf_epicprod/nersc_stage.py).

The access token is the IRI client file a PanDA operations cron refreshes
daily (``nersc.token_file``); its account is ``wguan``, a member of m3763.
Requests are JSON over HTTPS with the bearer token; an operation the API
runs asynchronously answers with a task id, which ``call`` waits on. The
token is never logged or returned in a record.
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

DEFAULT_TOKEN_FILE = '/data/wguan2/hpc_tokens/nersc_iri_config.yaml'
HTTP_TIMEOUT_S = 30
TASK_WAIT_S = 180


class IriError(RuntimeError):
    pass


def read_token_file(path):
    """(base_url, access_token, age in hours) from the IRI client file,
    a flat ``key: value`` file. Raises ValueError naming what is missing;
    the token itself is never logged or returned in a record."""
    values = {}
    with open(path) as f:
        for line in f:
            key, sep, value = line.partition(':')
            if sep and not key.lstrip().startswith('#'):
                values[key.strip()] = value.strip()
    missing = [k for k in ('base_url', 'access_token') if not values.get(k)]
    if missing:
        raise ValueError(f'{path} has no {" or ".join(missing)}')
    age_h = (time.time() - os.path.getmtime(path)) / 3600.0
    return values['base_url'].rstrip('/'), values['access_token'], round(age_h, 1)


def get(base_url, token, path, timeout=HTTP_TIMEOUT_S):
    req = urllib.request.Request(f'{base_url}{path}',
                                 headers={'Authorization': f'Bearer {token}',
                                          'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


class Client:
    """Filesystem and compute operations at NERSC under the token's account."""

    def __init__(self, token_file=DEFAULT_TOKEN_FILE):
        self.base, self.token, self.token_age_h = read_token_file(token_file)
        self._fs = {r['name']: r['id'] for r in self.call('GET', '/api/v1/filesystem/resources')}
        self._compute = {r['name']: r['id'] for r in self.call('GET', '/api/v1/compute/resources')}

    def call(self, method, path, params=None, body=None, content_type='application/json',
             timeout=HTTP_TIMEOUT_S):
        url = self.base + path + ('?' + urllib.parse.urlencode(params) if params else '')
        headers = {'Authorization': f'Bearer {self.token}', 'Accept': 'application/json'}
        if body is not None:
            headers['Content-Type'] = content_type
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                text = resp.read().decode()
        except urllib.error.HTTPError as exc:
            raise IriError(f'{method} {path}: HTTP {exc.code} {exc.read().decode()[:400]}') from None
        out = json.loads(text) if text else {}
        if isinstance(out, dict) and out.get('task_id') and set(out) <= {'task_id', 'task_uri'}:
            return self.wait(out['task_id'])
        return out

    def wait(self, task_id):
        deadline = time.time() + TASK_WAIT_S
        while time.time() < deadline:
            task = self.call('GET', f'/api/v1/task/{task_id}')
            status = str(task.get('status', '')).lower()
            if status == 'completed':
                return task.get('result')
            if status in ('failed', 'error', 'canceled'):
                raise IriError(f'task {task_id} {status}: {json.dumps(task)[:400]}')
            time.sleep(2)
        raise IriError(f'task {task_id} did not complete in {TASK_WAIT_S} s')

    # Filesystem operations run through the global common resource, which
    # sees /global/homes, /global/cfs and /global/common alike.
    def _fs_path(self, op):
        return f'/api/v1/filesystem/{op}/{self._fs["common"]}'

    def stat(self, path):
        """The path's stat record, or None when it does not exist."""
        try:
            return self.call('GET', self._fs_path('stat'), params={'path': path})['output']
        except IriError as exc:
            if 'No such file' in str(exc) or 'not found' in str(exc).lower():
                return None
            raise

    def mkdir(self, path):
        return self.call('POST', self._fs_path('mkdir'),
                         body=json.dumps({'path': path, 'parent': True}).encode())

    def chmod(self, path, mode):
        return self.call('PUT', self._fs_path('chmod'),
                         body=json.dumps({'path': path, 'mode': mode}).encode())

    def upload(self, path, data):
        """Write ``data`` (bytes) to ``path``, replacing it."""
        boundary = uuid.uuid4().hex
        name = path.rsplit('/', 1)[1]
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\n'
                'Content-Type: application/octet-stream\r\n\r\n').encode() + data + f'\r\n--{boundary}--\r\n'.encode()
        return self.call('POST', self._fs_path('upload'), params={'path': path}, body=body,
                         content_type=f'multipart/form-data; boundary={boundary}', timeout=120)

    def view(self, path):
        return self.call('GET', self._fs_path('view'), params={'path': path})['output']['content']

    def read(self, path):
        """The whole text of ``path``; raises IriError when the API answers
        with only part of it."""
        out = self.call('GET', self._fs_path('view'), params={'path': path})['output']
        content = out.get('content') or ''
        start, end = out.get('start_position'), out.get('end_position')
        if start is not None and end is not None and end - start != len(content.encode()):
            raise IriError(f'{path} read incompletely ({len(content.encode())} of {end - start} bytes)')
        return content

    # Compute: a Slurm job described in the API's job specification.
    def submit(self, spec):
        """Submit a job; returns its Slurm job id."""
        out = self.call('POST', f'/api/v1/compute/job/{self._compute["compute"]}',
                        body=json.dumps(spec).encode(), timeout=120)
        return str(out.get('id') or out.get('job_id') or out)

    def job(self, job_id):
        return self.call('GET', f'/api/v1/compute/status/{self._compute["compute"]}/{job_id}')
