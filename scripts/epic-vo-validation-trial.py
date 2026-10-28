#!/usr/bin/env python3
"""One 100-event managed epic-VO trial through authenticated PCS.

Default is a read-only plan. --apply creates a new trial of the proven
gun configuration, binds the BNL-XRD gun config, and requests submission.
Keep --state outside Git. Pending writes are never automatically resent.
The submit doer bounds a trial to one manifest row and its 100 events.
"""
import argparse
import json
import os
from pathlib import Path
import re
from urllib.parse import quote
import requests

SOURCE = 'group.EIC.26.07.3.epic_craterlake.p1108.e37.s11.r11.130to177deg'
SITE = 'BNL_OSG_PanDA_1'
BASE = 'https://epic-devcloud.org/prod'
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--apply', action='store_true')
parser.add_argument('--state', type=Path, required=True)
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

config = request('GET', '/pcs/api/prod-configs/23/')
if config['name'] != '26.07.3 Particle Gun' or config['rucio_rse'] != 'BNL-XRD':
    raise RuntimeError('Validation configuration identity changed')
if not state.get('task') and args.apply:
    if state.get('creation_pending'):
        raise RuntimeError('Prior creation outcome is uncertain; resolve it before continuing')
    state.update(creation_pending=True, actor=os.environ['SWF_REMOTE_CLAUDE_USER'])
    save()
    result = request('POST', task_path(SOURCE) + 'trial/', json={'events': 100, 'site': SITE})
    state.update(task=result['composed_name'], creation_pending=False)
    save()
if state.get('task'):
    task = request('GET', task_path(state['task']))
    if args.apply and not state.get('configured'):
        request('PATCH', task_path(state['task']), json={'prod_config': 23,
            'overrides': {**(task.get('overrides') or {}), 'task_priority': 1000},
            'description': 'One-job, 100-event managed epic-VO cutover validation approved by Torre.'})
        state['configured'] = True
        save()
    spec = request('GET', '/pcs/api/prod-tasks/command/', params={'name': state['task'], 'fmt': 'evgen'})
    if not (spec['vo'] == 'epic' and spec['prodSourceLabel'] == 'managed'
            and spec['site'] == SITE and spec['trial']['events'] == 100
            and spec['env']['OUT_RSE'] == 'BNL-XRD'):
        raise RuntimeError('The generated validation spec is outside its approved bounds')
    if args.apply and not state.get('submitted') and not task.get('panda_task_id'):
        if state.get('submission_pending'):
            raise RuntimeError('Prior submission outcome is uncertain; inspect the attempt first')
        state['submission_pending'] = True
        save()
        result = request('POST', task_path(state['task']) + 'submit/', json={'now': True, 'site': SITE})
        state.update(submitted=True, submission_pending=False)
        save()
    task = request('GET', task_path(state['task']))
    state['status'] = task.get('status')
    state['panda_task_id'] = task.get('panda_task_id')
    save()
print(json.dumps({'source': SOURCE, 'site': SITE, 'events': 100,
                  'vo': 'epic', 'prodSourceLabel': 'managed', 'jobs': 1, **state}))
