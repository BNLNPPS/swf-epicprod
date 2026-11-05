#!/usr/bin/env python3
"""Compose a trial of a PCS task for one site and output RSE, unsubmitted.

Default is a read-only plan. --apply composes the trial through
authenticated PCS (POST .../trial/), binds the named configuration, sets
the trial's overrides (default: none, the plain gun shape; --keep-overrides
keeps the source's), and checks the generated spec: the site, the output
RSE and the trial's events. With --like, the spec's keys and settings are
compared with an earlier trial's. Nothing is submitted; the operator
submits the trial from PCS. Keep --state outside Git; a pending write is
never automatically resent.
"""
import argparse
import json
import os
from pathlib import Path
import re
from urllib.parse import quote, urljoin

import requests

BASE = 'https://epic-devcloud.org/prod'
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--source', required=True, help='composed name of the task to trial')
parser.add_argument('--site', required=True, help='PanDA queue the trial runs on')
parser.add_argument('--config', required=True, help='name of the PCS configuration to bind')
parser.add_argument('--rse', required=True, help="the configuration's expected rucio_rse")
parser.add_argument('--events', type=int, default=100)
parser.add_argument('--description', default='')
parser.add_argument('--keep-overrides', action='store_true')
parser.add_argument('--like', default='', help='an earlier trial whose spec shape to compare')
parser.add_argument('--state', type=Path, required=True)
parser.add_argument('--apply', action='store_true')
args = parser.parse_args()
state = json.loads(args.state.read_text()) if args.state.exists() else {}

client = requests.Session()
login = client.get(BASE + '/accounts/login/', timeout=30)
login.raise_for_status()
csrf = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', login.text)
if not csrf:
    raise RuntimeError('No login CSRF token')
auth = client.post(login.url, data={'csrfmiddlewaretoken': csrf.group(1),
    'username': os.environ['SWF_REMOTE_CLAUDE_USER'],
    'password': os.environ['SWF_REMOTE_CLAUDE_PASSWORD']},
    headers={'Referer': login.url}, timeout=30)
auth.raise_for_status()
if '/accounts/login/' in auth.url or 'sessionid_prod' not in client.cookies:
    raise RuntimeError('Authenticated login did not complete')


def request(method, path, **kwargs):
    response = client.request(method, BASE + path, headers={
        'Referer': BASE + '/', 'X-CSRFToken': client.cookies.get('csrftoken_prod', '')},
        timeout=60, **kwargs)
    if not response.ok:
        raise RuntimeError(f'{method} {path}: HTTP {response.status_code}: {response.text[:1000]}')
    return response.json()


def save():
    args.state.write_text(json.dumps(state, indent=2) + '\n')
    args.state.chmod(0o600)


def task_path(name):
    return '/pcs/api/prod-tasks/' + quote(name, safe='') + '/'


def spec_of(name):
    return request('GET', '/pcs/api/prod-tasks/command/', params={'name': name, 'fmt': 'evgen'})


def flat(value, prefix=''):
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            out.update(flat(item, f'{prefix}{key}.'))
        return out
    return {prefix.rstrip('.'): value}


listing = request('GET', '/pcs/api/prod-configs/', params={'page_size': 500})
configs = [c for c in (listing.get('results', listing) if isinstance(listing, dict) else listing)
           if c.get('name') == args.config]
if len(configs) != 1 or configs[0].get('rucio_rse') != args.rse:
    raise RuntimeError(f'Configuration {args.config!r} with rucio_rse={args.rse} not found exactly once')
config_id = configs[0]['id']
request('GET', task_path(args.source))

if args.apply and not state.get('task'):
    if state.get('creation_pending'):
        raise RuntimeError('Prior creation outcome is uncertain; resolve it before continuing')
    state.update(creation_pending=True, source=args.source,
                 actor=os.environ['SWF_REMOTE_CLAUDE_USER'])
    save()
    result = request('POST', task_path(args.source) + 'trial/',
                     json={'events': args.events, 'site': args.site})
    state.update(task=result['composed_name'], url=urljoin(BASE + '/', result['url']), creation_pending=False)
    save()
if state.get('task'):
    task = request('GET', task_path(state['task']))
    if args.apply and not state.get('configured'):
        body = {'prod_config': config_id}
        if not args.keep_overrides:
            body['overrides'] = {}
        if args.description:
            body['description'] = args.description
        request('PATCH', task_path(state['task']), json=body)
        state['configured'] = True
        save()
    spec = spec_of(state['task'])
    checks = {'site': spec.get('site') == args.site,
              'OUT_RSE': (spec.get('env') or {}).get('OUT_RSE') == args.rse,
              'trial.events': (spec.get('trial') or {}).get('events') == args.events}
    state['checks'] = checks
    if args.like:
        mine, theirs = flat(spec), flat(spec_of(args.like))
        state['differs_from_like'] = {k: [theirs.get(k), mine.get(k)]
                                      for k in sorted(set(mine) | set(theirs))
                                      if mine.get(k) != theirs.get(k)}
    task = request('GET', task_path(state['task']))
    state.update(status=task.get('status'), panda_task_id=task.get('panda_task_id'),
                 prod_config=task.get('prod_config'))
    save()
    if not all(checks.values()):
        raise RuntimeError(f'The generated spec is outside its bounds: {checks}')
print(json.dumps({'source': args.source, 'site': args.site, 'config': args.config,
                  'config_id': config_id, 'events': args.events, **state}, indent=2, default=str))
