#!/usr/bin/python3.12
"""pcs-submit.py: one command for PCS task operations through the REST API.

Standalone (stdlib only): reads SWF_API_TOKEN from the environment or ~/.env
and talks to the monitor's PCS API, so a submission is one command with no
shell plumbing around it. Every call runs as the token's account and lands
in the action stream under that name.

    pcs-submit.py trial  <task> [--events N] [--site QUEUE] [--input-did DID]   compose a trial of <task> and submit it
    pcs-submit.py submit <task>                                                 submit <task> (a composed task or trial)
    pcs-submit.py residual <task>                                               queue a residual .tryN rerun of <task>
    pcs-submit.py status <task>                                                 the task's status and PanDA tasks

<task> is a composed name (group.EIC.26.07.1.epic_craterlake.p2445.e49.s9.r9)
or a trial name. SWF_MONITOR_URL overrides the base
(default https://localhost/swf-monitor, the host's own face).
"""
import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get('SWF_MONITOR_URL', 'https://localhost/swf-monitor').rstrip('/')


def token():
    t = os.environ.get('SWF_API_TOKEN')
    if t:
        return t
    env = os.path.expanduser('~/.env')
    if os.path.exists(env):
        for line in open(env):
            line = line.strip()
            if line.startswith('export SWF_API_TOKEN='):
                return line.split('=', 1)[1].strip().strip('"').strip("'")
    sys.exit('SWF_API_TOKEN not in the environment or ~/.env')


def call(method, path, body=None):
    ctx = ssl.create_default_context()
    if BASE.startswith('https://localhost'):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    data = json.dumps(body or {}).encode() if method != 'GET' else None
    req = urllib.request.Request(f'{BASE}{path}', data=data, method=method, headers={
        'Authorization': f'Token {token()}', 'Content-Type': 'application/json',
        'Accept': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=120, context=ctx) as r:
            return r.status, json.loads(r.read().decode() or '{}')
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:                                     # noqa: BLE001
            return e.code, {'detail': str(e)}


def task_path(name, action=''):
    return f'/pcs/api/prod-tasks/{urllib.parse.quote(name, safe="")}/' + (f'{action}/' if action else '')


def show_status(name):
    code, d = call('GET', task_path(name))
    if code != 200:
        sys.exit(f'{name}: HTTP {code} {d.get("detail", d)}')
    rows = [(p.get('jedi_task_id'), p.get('try_number'), (p.get('metadata') or {}).get('payload_version', ''))
            for p in d.get('panda_tasks', [])]
    print(f"{name}: status {d.get('status')}, panda_task_id {d.get('panda_task_id')}, "
          f"PanDA tasks {rows}")
    return d


def submit(name, wait=90):
    code, d = call('POST', task_path(name, 'submit'))
    if code >= 300:
        sys.exit(f'submit {name}: HTTP {code} {d.get("detail", d)}')
    if d.get('warnings'):
        print(f'warnings: {d["warnings"]}')
    print(f'{name}: submission queued to the ops agent; waiting for the JEDI task id')
    deadline = time.time() + wait
    while time.time() < deadline:
        time.sleep(5)
        code, d = call('GET', task_path(name))
        if code == 200 and d.get('panda_task_id'):
            print(f"{name}: submitted, JEDI task {d['panda_task_id']}")
            return d
    print(f'{name}: no JEDI task id after {wait} s; check the action stream')
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    sub = ap.add_subparsers(dest='cmd', required=True)
    t = sub.add_parser('trial'); t.add_argument('task')
    t.add_argument('--events', type=int); t.add_argument('--site', default='')
    t.add_argument('--input-did', default='')
    s = sub.add_parser('submit'); s.add_argument('task')
    r = sub.add_parser('residual'); r.add_argument('task')
    st = sub.add_parser('status'); st.add_argument('task')
    a = ap.parse_args()
    if a.cmd == 'status':
        show_status(a.task)
    elif a.cmd == 'submit':
        submit(a.task)
    elif a.cmd == 'residual':
        code, d = call('POST', task_path(a.task, 'rerun-residual'))
        if code >= 300:
            sys.exit(f'residual {a.task}: HTTP {code} {d.get("detail", d)}')
        print(f'{a.task}: residual rerun queued to the ops agent')
    elif a.cmd == 'trial':
        body = {'site': a.site, 'input_did': a.input_did}
        if a.events:
            body['events'] = a.events
        code, d = call('POST', task_path(a.task, 'trial'), body)
        if code >= 300:
            sys.exit(f'trial of {a.task}: HTTP {code} {d.get("detail", d)}')
        name = d['composed_name']
        print(f"trial composed: {name} (config {d.get('prod_config')})")
        submit(name)


if __name__ == '__main__':
    main()
