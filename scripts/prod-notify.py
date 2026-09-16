#!/usr/bin/env python3
"""prod-notify.py: production activity and health, spoken into the working
session when something changes (EPICPROD_OPS.md, Production notices).

A cron script with no Django. It reads the production record through the
monitor's MCP tools over the loopback endpoint, keeps a small state file
so that each condition speaks once when it appears and once when it
clears, and delivers each notice to the operators' working sessions:
through TJAI messaging to the sessions on this host, and through
TeamComms Notify LLM to the `prod-notify` topic when a connector
configuration is given. Silence otherwise; no heartbeat.

Triggers:

  task.start / task.end   a production task (processingtype
                          epicproduction) appears or reaches a terminal
                          state; the end notice carries the counts
  task.failing            a running production task's failure rate over
                          its completed jobs passes FAIL_FRACTION with at
                          least FAIL_MIN_JOBS completed (again when it
                          doubles)
  queue.starved           a queue holds more than STARVED_ACTIVATED
                          activated jobs with nothing running for
                          STARVED_MINUTES
  platform.error          a production platform check turns error
  credential.expiry       a production credential has fewer than
                          CREDENTIAL_DAYS days left
  arrivals.missing        a production task finished more than
                          ARRIVALS_HOURS ago and one of its pre-created
                          output datasets holds fewer files in JLab Rucio
                          than the task has finished jobs (checked once;
                          arrivals.complete when the count catches up)
  nodeguard.trip          the node guard trips on a node for the first
                          time

Usage (cron, every five minutes)::

    source ~/.env && python3 scripts/prod-notify.py [--dry-run] [--state PATH]
        [--teamcomms-config /path/to/program.json]

The environment supplies SWF_MONITOR_MCP_TOKEN (the monitor MCP),
SWF_MONITOR_URL (the monitor's REST face, read anonymously for the PCS
task record) and TJAI_MCP_TOKEN (delivery). --dry-run prints the notices
and writes no state.
"""
import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import time
import urllib.request
import uuid

MONITOR_MCP_URL = os.environ.get('SWF_MONITOR_MCP_URL',
                                 'http://127.0.0.1:8001/swf-monitor/mcp/')
MONITOR_URL = os.environ.get('SWF_MONITOR_URL',
                             'https://pandaserver02.sdcc.bnl.gov/swf-monitor').rstrip('/')
TJAI_MCP_URL = os.environ.get('TJAI_MCP_URL', 'https://etaverse.com/tjai/mcp/')
LOCATION = os.environ.get('TJAI_LOCATION_NAME', 'swf-testbed')
TJAI_RESOURCE = f'host:{LOCATION}'
FACE = 'https://epic-devcloud.org/prod'
STATE_DEFAULT = '/data/wenauseic/swf-epicprod/prod-notify-state.json'
SOURCE = 'swf-epicprod:prod-notify'

FAIL_FRACTION = 0.10
FAIL_MIN_JOBS = 200
STARVED_ACTIVATED = 100
STARVED_MINUTES = 30
CREDENTIAL_DAYS = 14
ARRIVALS_HOURS = 2
ARRIVALS_RECHECK_MINUTES = 60
TERMINAL = ('done', 'finished', 'failed', 'broken', 'aborted', 'exhausted')
SUCCEEDED = ('done', 'finished')


def _mcp(url, token, tool, arguments, timeout=90):
    body = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
            'params': {'name': tool, 'arguments': arguments}}
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method='POST',
        headers={'Authorization': f'Bearer {token}',
                 'Content-Type': 'application/json',
                 'Accept': 'application/json, text/event-stream'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        resp = json.loads(r.read().decode())
    if 'error' in resp:
        raise RuntimeError(f'{tool}: MCP error {resp["error"]}')
    res = resp['result']
    text = ''.join(c.get('text', '') for c in res.get('content', []))
    if res.get('isError'):
        raise RuntimeError(f'{tool}: tool error {text[:400]}')
    return text


def monitor(tool, **arguments):
    token = os.environ.get('SWF_MONITOR_MCP_TOKEN', '')
    if not token:
        raise RuntimeError('SWF_MONITOR_MCP_TOKEN not in the environment')
    return json.loads(_mcp(MONITOR_MCP_URL, token, tool, arguments))


# ---------------------------------------------------------------- readings

def read_tasks():
    """Production tasks touched in the last two days: id -> record."""
    out = monitor('panda_list_tasks', days=2, processingtype='epicproduction',
                  limit=200)
    return {int(t['jeditaskid']): t for t in out.get('tasks', [])}


def read_activity():
    return monitor('panda_get_activity', days=1)


def read_workers():
    return monitor('panda_harvester_workers', hours=1)


def read_campaign():
    return monitor('epicprod_campaign_status', window_days=1)


def read_nodeguard():
    try:
        with urllib.request.urlopen(
                f'{FACE}/panda/node-guard/?format=json', timeout=30) as r:
            return json.loads(r.read().decode())
    except Exception:  # noqa: BLE001
        return None


def read_output_datasets(taskname, tid):
    """The DIDs PCS pre-created for this PanDA task, from the task's PCS
    record (`PandaTasks.metadata.output_datasets`, read over the monitor's
    REST face); empty for a task submitted without them."""
    url = f'{MONITOR_URL}/pcs/api/prod-tasks/{taskname}/'
    with urllib.request.urlopen(url, timeout=30) as r:
        rec = json.loads(r.read().decode())
    for pt in rec.get('panda_tasks') or []:
        if int(pt.get('jedi_task_id') or 0) == int(tid):
            return [d['dataset'] for d in (pt.get('metadata') or {}).get('output_datasets') or []
                    if d.get('dataset')]
    return []


def count_rucio_files(did):
    """Files registered in a JLab Rucio dataset (an open dataset carries no
    length in its metadata; the file listing's total is the count)."""
    scope, name = did.split(':', 1)
    out = monitor('jlab_rucio_list_files', scope=scope, name=name, limit=1)
    if int(out.get('status') or 0) != 200:
        raise RuntimeError(f'jlab_rucio_list_files {did}: status {out.get("status")}')
    return int((out.get('pagination') or {}).get('total_count') or 0)


# ---------------------------------------------------------------- triggers

def _fmt_task(t):
    return (f"{t['taskname']} (task {t['jeditaskid']}, {t.get('username', '')}, "
            f"{t.get('site') or 'no site'})")


def task_triggers(tasks, state):
    """task.start, task.end and task.failing from the task census."""
    notices = []
    known = state.setdefault('tasks', {})
    for tid, t in sorted(tasks.items()):
        key = str(tid)
        prev = known.get(key) or {}
        status = str(t.get('status') or '')
        fin, fail = int(t.get('nfinished') or 0), int(t.get('nfailed') or 0)
        active = int(t.get('nactive') or 0)
        if not prev:
            notices.append(('task.start', f'{tid}:{status}',
                            f'Production task started: {_fmt_task(t)}; '
                            f'{fin + fail + active} jobs, {int(t.get("nrunning") or 0)} running.',
                            f'{FACE}/panda/tasks/{tid}/'))
        elif status in TERMINAL and prev.get('status') not in TERMINAL:
            rate = fail / (fin + fail) if (fin + fail) else 0.0
            notices.append(('task.end', f'{tid}:{status}',
                            f'Production task {status}: {_fmt_task(t)}; '
                            f'{fin} finished, {fail} failed ({rate:.1%}), '
                            f'{int(t.get("nfinalfailed") or 0)} final failures.',
                            f'{FACE}/panda/tasks/{tid}/'))
        if status not in TERMINAL and (fin + fail) >= FAIL_MIN_JOBS:
            rate = fail / (fin + fail)
            last = float(prev.get('reported_rate') or 0.0)
            if rate >= FAIL_FRACTION and (not last or rate >= 2 * last):
                notices.append(('task.failing', f'{tid}:{rate:.2f}',
                                f'Production task failing: {_fmt_task(t)}; '
                                f'{fail} of {fin + fail} completed jobs failed ({rate:.1%}).',
                                f'{FACE}/panda/tasks/{tid}/'))
                prev = dict(prev, reported_rate=rate)
        known[key] = dict(prev, status=status, seen=_now())
    # forget tasks the census no longer returns (older than two days)
    for key in list(known):
        if int(key) not in tasks and _age_hours(known[key].get('seen')) > 72:
            del known[key]
    return notices


def queue_triggers(activity, workers, state):
    """queue.starved: activated work with nothing running for a while."""
    notices = []
    starved = state.setdefault('starved', {})
    by_site = {s['site']: s for s in activity.get('jobs', {}).get('by_site', [])}
    for site, row in by_site.items():
        activated = int(row.get('activated') or 0)
        running = int(row.get('running') or 0) + int(row.get('starting') or 0)
        if activated > STARVED_ACTIVATED and running == 0:
            since = starved.get(site) or _now()
            starved[site] = since
            if _age_minutes(since) >= STARVED_MINUTES and not starved.get(f'{site}:told'):
                nw = (workers.get('by_site') or {}).get(site, 0)
                notices.append(('queue.starved', site,
                                f'Queue starved: {site} holds {activated} activated jobs '
                                f'with none running for {int(_age_minutes(since))} min; '
                                f'{nw} harvester workers in the last hour.',
                                f'{FACE}/panda/queues/{site}/'))
                starved[f'{site}:told'] = _now()
        else:
            if starved.get(f'{site}:told'):
                notices.append(('queue.recovered', site,
                                f'Queue running again: {site}, {running} running, '
                                f'{activated} activated.', f'{FACE}/panda/queues/{site}/'))
            starved.pop(site, None)
            starved.pop(f'{site}:told', None)
    return notices


def platform_triggers(campaign, state):
    """platform.error, credential.expiry, arrivals.missing from the
    campaign status document (production checks only)."""
    notices = []
    members = campaign.get('members') or {}
    system = (members.get('system_status') or {}).get('data') or {}
    errors = sorted(r['name'] for r in (system.get('current_non_ok') or [])
                    if str(r.get('status')) == 'error')
    told = set(state.get('platform_errors') or [])
    for name in errors:
        if name not in told:
            notices.append(('platform.error', name,
                            f'Production platform check {name} is red.',
                            f'{FACE}/system/'))
    for name in told - set(errors):
        notices.append(('platform.recovered', name,
                        f'Production platform check {name} is back.', f'{FACE}/system/'))
    state['platform_errors'] = errors

    creds = ((members.get('credential_status') or {}).get('data') or {})
    days = (creds.get('details') or {}).get('days_left') or {}
    told = state.setdefault('credentials', {})
    for cred, left in days.items():
        try:
            left = float(left)
        except (TypeError, ValueError):
            continue
        if left < CREDENTIAL_DAYS and not told.get(cred):
            notices.append(('credential.expiry', cred,
                            f'Credential {cred} expires in {left:.1f} days.',
                            f'{FACE}/alarms/'))
            told[cred] = _now()
        elif left >= CREDENTIAL_DAYS:
            told.pop(cred, None)
    return notices


def arrivals_triggers(tasks, state, failures):
    """arrivals.missing / arrivals.complete: a finished task's pre-created
    output datasets in JLab Rucio against its finished jobs, read once
    ARRIVALS_HOURS after the end and then every ARRIVALS_RECHECK_MINUTES
    while a dataset is short. A task without pre-created datasets has
    nothing to check and is recorded as such. A failed read is reported
    and retried next run."""
    notices = []
    record = state.setdefault('arrivals', {})
    for tid, t in sorted(tasks.items()):
        key = str(tid)
        if str(t.get('status')) not in SUCCEEDED or not t.get('endtime'):
            continue
        if _age_hours(t['endtime']) < ARRIVALS_HOURS:
            continue
        prev = record.get(key)
        if isinstance(prev, str):
            # state written by the earlier form of this trigger: told, unresolved
            prev = {'checked': prev, 'missing': True}
        if prev and not prev.get('missing'):
            continue
        if prev and _age_minutes(prev.get('checked')) < ARRIVALS_RECHECK_MINUTES:
            continue
        nfin = int(t.get('nfinished') or 0)
        link = f'{FACE}/panda/tasks/{tid}/'
        try:
            dids = read_output_datasets(t['taskname'], tid)
            counts = {did: count_rucio_files(did) for did in dids}
        except Exception as e:  # noqa: BLE001
            failures.append(f'arrivals {tid}: {e}')
            continue
        short = sorted((did, n) for did, n in counts.items() if n < nfin)
        if short and not prev:
            did, n = short[0]
            more = f' (and {len(short) - 1} more)' if len(short) > 1 else ''
            notices.append(('arrivals.missing', key,
                            f'Outputs missing in Rucio {ARRIVALS_HOURS} h after '
                            f'{_fmt_task(t)} finished: {n} of {nfin} files in {did}{more}.',
                            link))
        elif prev and not short:
            notices.append(('arrivals.complete', key,
                            f'Outputs complete in Rucio: {_fmt_task(t)}; '
                            f'{nfin} files in each of {len(counts)} dataset(s).', link))
        record[key] = {'checked': _now(), 'missing': bool(short), 'datasets': len(dids)}
    for key in list(record):
        if int(key) not in tasks:
            del record[key]
    return notices


def nodeguard_triggers(record, state):
    """nodeguard.trip: the first trip of any node."""
    notices = []
    if not isinstance(record, dict):
        return notices
    told = set(state.get('nodeguard') or [])
    trips = []
    for row in record.get('nodes') or record.get('excluded') or []:
        node = row.get('node') or row.get('name')
        if node and str(row.get('status') or row.get('state') or '') in ('excluded', 'tripped'):
            trips.append(node)
    for node in trips:
        if node not in told:
            notices.append(('nodeguard.trip', node,
                            f'Node guard tripped on {node}.', f'{FACE}/panda/node-guard/'))
    state['nodeguard'] = sorted(set(trips) | told)
    return notices


# ---------------------------------------------------------------- delivery

def deliver_tjai(text):
    token = os.environ.get('TJAI_MCP_TOKEN', '')
    if not token:
        raise RuntimeError('TJAI_MCP_TOKEN not in the environment')
    reg = json.loads(_mcp(TJAI_MCP_URL, token, 'register_session', dict(
        client='epicprod', name='prod-notify', host=LOCATION,
        cwd='/data/wenauseic/github/swf-epicprod', native_id=str(uuid.uuid4()),
        model='', resources=[TJAI_RESOURCE])))
    sender = reg.get('id') or reg.get('session_id')
    if not sender:
        raise RuntimeError(f'register_session returned no id: {str(reg)[:200]}')
    _mcp(TJAI_MCP_URL, token, 'send_message', {
        'sender_id': sender, 'resource': TJAI_RESOURCE,
        'message_id': str(uuid.uuid4()), 'content': text,
        'reply_requested': False})


def deliver_teamcomms(config, trigger, key, text, link, observed_at):
    """One Notify LLM to the prod-notify topic through the connector CLI;
    the helper keeps the outgoing queue and retries under `flush`."""
    payload = {
        'source': SOURCE,
        'event_id': f'{trigger}:{key}:{observed_at}',
        'reason': f'Production notice, trigger {trigger}: for the operators\' working sessions.',
        'content': f'{text}\n{link}',
        'audience': {'topics': ['prod-notify']},
        'topic': 'prod-notify',
        'observed_at': observed_at,
    }
    cmd = [os.environ.get('TEAMCOMMS_CONNECT',
                          '/opt/swf-monitor/current/.venv/bin/teamcomms-connect'),
           '--config', config, 'notify-llm', '-']
    p = subprocess.run(cmd, input=json.dumps(payload), capture_output=True,
                       text=True, timeout=60)
    if p.returncode != 0:
        raise RuntimeError(f'notify-llm exit {p.returncode}: {(p.stderr or p.stdout)[-300:]}')


# ---------------------------------------------------------------- helpers

def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')


def _parse(s):
    try:
        v = dt.datetime.fromisoformat(str(s).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    return v if v.tzinfo else v.replace(tzinfo=dt.timezone.utc)


def _age_hours(s):
    v = _parse(s)
    return 1e9 if v is None else (dt.datetime.now(dt.timezone.utc) - v).total_seconds() / 3600


def _age_minutes(s):
    return _age_hours(s) * 60


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--state', default=STATE_DEFAULT)
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--teamcomms-config', default=os.environ.get('PROD_NOTIFY_TEAMCOMMS_CONFIG', ''))
    args = ap.parse_args(argv)

    state = {}
    if os.path.exists(args.state):
        with open(args.state) as f:
            state = json.load(f)
    first_run = not state.get('tasks')

    tasks = read_tasks()
    activity = read_activity()
    workers = read_workers()
    campaign = read_campaign()
    nodeguard = read_nodeguard()

    failures = []
    notices = []
    notices += task_triggers(tasks, state)
    notices += queue_triggers(activity, workers, state)
    notices += platform_triggers(campaign, state)
    notices += arrivals_triggers(tasks, state, failures)
    notices += nodeguard_triggers(nodeguard, state)
    if first_run:
        # The first pass seeds the state; the record's standing conditions
        # are not news.
        notices = []
    state['last_run'] = _now()

    for trigger, key, text, link in notices:
        line = f'[prod-notify {trigger}] {text} {link}'
        print(line)
        if args.dry_run:
            continue
        try:
            deliver_tjai(line)
        except Exception as e:  # noqa: BLE001
            failures.append(f'tjai {trigger}:{key}: {e}')
        if args.teamcomms_config:
            try:
                deliver_teamcomms(args.teamcomms_config, trigger, key, text, link,
                                  state['last_run'])
            except Exception as e:  # noqa: BLE001
                failures.append(f'teamcomms {trigger}:{key}: {e}')
    if not args.dry_run:
        os.makedirs(os.path.dirname(args.state), exist_ok=True)
        tmp = args.state + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(state, f, indent=1, sort_keys=True)
        os.replace(tmp, args.state)
    for f in failures:
        print(f'FAILED: {f}', file=sys.stderr)
    print(f'prod-notify: {len(notices)} notice(s)' + (' (seeded)' if first_run else ''))
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
