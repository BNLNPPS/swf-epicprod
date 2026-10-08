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
- ``nersc.queues``: {queue: project} for the production queues each
  project's CPU allocation pays for.
- ``nersc.close_at_used`` (0.9): the fraction of a project's CPU
  node-hours used at which its queues close.

Each readable balance sets the queues (Torre, 2026-10-08): a queue whose
project has used ``close_at_used`` or more is closed in
``front.closed_queues`` with a reason starting ``NERSC allocation:``; a
queue closed for allocation (that reason, or an operator's reason naming
the allocation) reopens when a new balance brings usage below it. A
queue an operator closed for any other reason is left alone, and an
unreadable balance changes nothing.
"""
import json
import logging
import os
import time
import urllib.error
import urllib.request

from django.utils import timezone

logger = logging.getLogger(__name__)

DEFAULT_TOKEN_FILE = '/data/wguan2/hpc_tokens/nersc_iri_config.yaml'
DEFAULT_PROJECTS = ['m3763']
DEFAULT_QUEUES = {'NERSC_Perlmutter_epic': 'm3763', 'NERSC_Perlmutter_epic_es': 'm3763'}
DEFAULT_CLOSE_AT_USED = 0.9
RULE_PREFIX = 'NERSC allocation: '
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


def _allocation_reason(reason):
    """True when a closed queue's reason is the allocation: the rule's
    own, or an operator's that names it."""
    reason = str(reason or '')
    return reason.startswith(RULE_PREFIX) or 'allocation' in reason.lower()


def queue_switches(record, queues, close_at, closed):
    """The queue changes a balance calls for. Pure.

    ``queues`` {queue: project}; ``closed`` the current
    ``front.closed_queues``. Returns [{queue, project, used, action,
    reason}] with action 'close' or 'open', only where the state
    changes; a project without a readable CPU balance decides nothing."""
    out = []
    if not record or record.get('error'):
        return out
    for queue, project in sorted(queues.items()):
        cpu = ((record.get('projects') or {}).get(project) or {}).get('cpu') or {}
        if not cpu.get('allocation'):
            continue
        used = cpu['usage'] / cpu['allocation']
        current = closed.get(queue)
        if used >= close_at:
            reason = f'{RULE_PREFIX}{project} CPU {close_at:.0%} or more used'
            if current is None or (_allocation_reason(current) and current != reason):
                out.append({'queue': queue, 'project': project, 'used': used,
                            'action': 'close', 'reason': reason})
        elif current is not None and _allocation_reason(current):
            out.append({'queue': queue, 'project': project, 'used': used,
                        'action': 'open', 'reason': current})
    return out


def apply_queue_switches(record, created_by):
    """Close or reopen the NERSC queues from the balance, under a row
    lock so a concurrent operator edit of the closed queues is not lost;
    each change is a ``nersc_queue_switch`` action. Returns the changes."""
    from django.db import transaction
    from monitor_app.epicprod_logging import log_epicprod_action
    from monitor_app.models import SysConfig

    queues = SysConfig.get_setting('nersc.queues', DEFAULT_QUEUES)
    close_at = SysConfig.get_setting('nersc.close_at_used', DEFAULT_CLOSE_AT_USED)
    if not (isinstance(queues, dict) and all(isinstance(v, str) for v in queues.values())):
        logger.error('nersc.queues is not a {queue: project} mapping: %r; using the defaults', queues)
        queues = dict(DEFAULT_QUEUES)
    if not isinstance(close_at, (int, float)) or not 0 < close_at <= 1:
        logger.error('nersc.close_at_used is not a fraction in (0, 1]: %r; using %s',
                     close_at, DEFAULT_CLOSE_AT_USED)
        close_at = DEFAULT_CLOSE_AT_USED
    with transaction.atomic():
        obj, _ = SysConfig.objects.select_for_update().get_or_create(
            id=1, defaults={'config_data': {}})
        closed = obj.config_data.get('front.closed_queues')
        if not isinstance(closed, dict):
            logger.error('front.closed_queues is not a mapping: %r; queues left unchanged', closed)
            return []
        changes = queue_switches(record, queues, close_at, closed)
        if not changes:
            return []
        closed = dict(closed)
        for c in changes:
            if c['action'] == 'close':
                closed[c['queue']] = c['reason']
            else:
                closed.pop(c['queue'], None)
        obj.config_data['front.closed_queues'] = closed
        obj.updated_by = created_by
        obj.save()
    for c in changes:
        verb = 'closed' if c['action'] == 'close' else 'reopened'
        log_epicprod_action(
            'ops-agent', 'nersc_queue_switch', username=created_by, outcome='ok',
            sublevel='normal', subject_type='queue', subject_key=c['queue'],
            message=(f"{verb} {c['queue']}: {c['project']} CPU {c['used']:.1%} used "
                     f"(closes at {close_at:.0%})"),
            queue=c['queue'], project=c['project'], action_taken=c['action'],
            used_fraction=round(c['used'], 4), close_at_used=close_at,
            previous_reason=c['reason'] if c['action'] == 'open' else '')
    return changes


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
    if not record['error']:
        record['queue_switches'] = apply_queue_switches(record, created_by)
    return record
