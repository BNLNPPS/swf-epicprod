"""The NERSC allocation balance (docs/CONTINUOUS_PRODUCTION.md, Placement,
Closed queues): the node-hours each NERSC project production charges
has left, read from the NERSC IRI API with an access token a PanDA
operations cron refreshes daily into a file on this host.

The read runs as the production-operations agent's
``nersc_allocation_read`` doer (swf-monitor
``scripts/nersc-allocation-read.py``), hourly by cron enqueue; the token
never leaves the agent's side. The result is stored as the cached
product ``nersc_allocation`` for the front page, and each read is a
``nersc_allocation_read`` action. A project the token's owner is not a
member of reads ``not visible to the token``: the IRI project list
holds only the owner's projects.

Settings in SysConfig, seeded at their defaults on first read:

- ``nersc.token_file``: the IRI client configuration file holding
  ``base_url`` and ``access_token``.
- ``nersc.projects`` (['m3763']): the projects whose balance is read.

Nothing here opens or closes a queue; ``front.closed_queues`` stays the
operator's until the balance is readable and a rule is set.
"""
import json
import os
import time
import urllib.error
import urllib.request

from django.utils import timezone

DEFAULT_TOKEN_FILE = '/data/wguan2/hpc_tokens/nersc_iri_config.yaml'
DEFAULT_PROJECTS = ['m3763']
PRODUCT_KEY = 'nersc_allocation'
PRODUCT_TTL_S = 7 * 24 * 3600
HTTP_TIMEOUT_S = 30
CAPABILITIES = ('cpu', 'gpu')


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


def _get(base_url, token, path):
    req = urllib.request.Request(f'{base_url}{path}',
                                 headers={'Authorization': f'Bearer {token}',
                                          'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode())


def summarize(projects, allocations, wanted):
    """Per wanted project, its CPU and GPU node-hours. Pure.

    ``projects`` is the IRI project list; ``allocations`` {project id:
    its project_allocations list}; ``wanted`` the project names."""
    by_name = {p.get('name'): p for p in projects if isinstance(p, dict)}
    out = {}
    for name in wanted:
        project = by_name.get(name)
        if project is None:
            out[name] = {'visible': False}
            continue
        block = {'visible': True, 'id': project.get('id'),
                 'description': project.get('description', '')}
        for alloc in allocations.get(project.get('id')) or []:
            capability = str(alloc.get('capability_uri') or '').rstrip('/').rsplit('/', 1)[-1]
            if capability not in CAPABILITIES:
                continue
            for entry in alloc.get('entries') or []:
                if entry.get('unit') != 'node_hours':
                    continue
                total = float(entry.get('allocation') or 0.0)
                used = float(entry.get('usage') or 0.0)
                block[capability] = {'allocation': round(total, 1), 'usage': round(used, 1),
                                     'remaining': round(total - used, 1)}
        out[name] = block
    return out


def read_balance(token_file, wanted):
    """The balance record: per wanted project its node-hours, the
    projects the token sees, the token file's age, or the error."""
    record = {'read_at': timezone.now().isoformat(), 'token_file': token_file,
              'projects': {}, 'visible_projects': [], 'error': ''}
    try:
        base_url, token, age_h = read_token_file(token_file)
        record['token_age_h'] = age_h
        projects = _get(base_url, token, '/api/v1/account/projects')
        if not isinstance(projects, list):
            raise ValueError(f'project list is not a list: {str(projects)[:200]}')
        record['visible_projects'] = sorted(p.get('name', '') for p in projects
                                            if isinstance(p, dict))
        allocations = {}
        for p in projects:
            if isinstance(p, dict) and p.get('name') in wanted:
                allocations[p['id']] = _get(
                    base_url, token,
                    f"/api/v1/account/projects/{p['id']}/project_allocations")
        record['projects'] = summarize(projects, allocations, wanted)
    except urllib.error.HTTPError as exc:
        record['error'] = f'IRI API HTTP {exc.code}: {exc.reason}'
    except (OSError, ValueError, urllib.error.URLError) as exc:
        record['error'] = f'{type(exc).__name__}: {exc}'
    return record


def line(record):
    """One line per project for a page: the balance, or why there is none."""
    if not record:
        return ['not read yet']
    if record.get('error'):
        return [f"unreadable: {record['error']}"]
    out = []
    for name, p in sorted((record.get('projects') or {}).items()):
        if not p.get('visible'):
            seen = ', '.join(record.get('visible_projects') or []) or 'none'
            out.append(f'{name}: not visible to the token (it sees {seen})')
            continue
        parts = [f"{cap.upper()} {p[cap]['remaining']:,.0f} of {p[cap]['allocation']:,.0f} "
                 f"node-hours left" for cap in CAPABILITIES if cap in p]
        out.append(f"{name}: {'; '.join(parts) or 'no node-hour allocation'}")
    return out


def run(created_by='nersc_allocation'):
    """Read, store the cached product, record the read. Returns the record."""
    from monitor_app.cached_product import get_product
    from monitor_app.epicprod_logging import log_epicprod_action
    from monitor_app.models import SysConfig

    token_file = str(SysConfig.get_setting('nersc.token_file', DEFAULT_TOKEN_FILE))
    wanted = SysConfig.get_setting('nersc.projects', DEFAULT_PROJECTS)
    if not (isinstance(wanted, list) and all(isinstance(w, str) for w in wanted)):
        wanted = list(DEFAULT_PROJECTS)
    record = read_balance(token_file, wanted)
    get_product(PRODUCT_KEY, lambda: record, ttl_seconds=PRODUCT_TTL_S, refresh=True)
    log_epicprod_action(
        'ops-agent', 'nersc_allocation_read', username=created_by,
        outcome='error' if record['error'] else 'ok',
        sublevel='normal' if record['error'] else 'low',
        message='NERSC allocation: ' + ' | '.join(line(record)),
        **{k: v for k, v in record.items() if k != 'token_file'})
    return record
