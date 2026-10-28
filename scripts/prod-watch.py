#!/usr/bin/env python3
"""prod-watch.py: the production watcher's tick (EPICPROD_WATCHER.md).

A cron script with no Django and no model, every thirty minutes. It reads
the room's stored snapshot through the monitor's MCP tools, opens an item
for each open problem, reads the watcher's outcome records from the action
stream, wakes the watcher session when an item is new, changed, or out of
its wait, and escalates an item with no outcome by the second tick.

Sources: the room's report lines at amber or red, its live screen at
warning or above, tasks paused in the last two days, and promises
(``watch_promise`` records) come due.

Escalation is shadowed (recorded, not live) while ``--escalate`` is not
given; the first day measures its volume.

    source ~/.env && python3 scripts/prod-watch.py [--dry-run] [--escalate]
        [--state PATH] [--session NAME]

Environment: SWF_MONITOR_MCP_TOKEN, SWF_MONITOR_HTTP_URL and SWF_API_TOKEN
(the monitor), TJAI_MCP_TOKEN (the wake).
"""
import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import os
import re
import sys
import urllib.error
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location('prod_notify', os.path.join(HERE, 'prod-notify.py'))
pn = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pn)

STATE_DEFAULT = '/data/wenauseic/swf-epicprod/prod-watch-state.json'
SESSION = 'swf-watch'
TICK_MINUTES = 30
# An item that clears and comes back within this time, with the same
# evidence, is the same item: its first sighting and its outcome stand.
GONE_HOURS = 2
ESCALATE_TICKS = 2
PUSH_REPEAT_HOURS = 24
PAUSED_HOURS = 48
LIVE_LEVELS = ('WARNING', 'ERROR', 'CRITICAL')
FACE = 'https://epic-devcloud.org/prod'


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _iso(t):
    return t.isoformat(timespec='seconds')


def _parse(s):
    try:
        return dt.datetime.fromisoformat(str(s).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None


def _digest(*parts):
    return hashlib.sha1('|'.join(str(p) for p in parts).encode()).hexdigest()[:12]


def report_key(item):
    link = item.get('link') or ''
    m = re.search(r'/alarms/events/([0-9a-f-]+)/', link)
    if m:
        return f'alarm:{m.group(1)}'
    subject = (item.get('text') or '').split(': ', 1)[0]
    return 'report:' + re.sub(r'\d+(\.\d+)?%?', '#', subject).strip()


def read_items(snapshot, promises, now):
    """{key: {source, level, text, link, digest}} for every open item."""
    items = {}
    for it in ((snapshot.get('report') or {}).get('items') or []):
        if it.get('level') not in ('amber', 'red'):
            continue
        key = report_key(it)
        items[key] = {'source': 'room report', 'level': it['level'], 'text': it.get('text', ''),
                      'link': it.get('link') or '', 'digest': _digest(it['level'], key)}
    for it in ((snapshot.get('live') or {}).get('items') or []):
        if str(it.get('level', '')).upper() not in LIVE_LEVELS:
            continue
        key = f"live:{it.get('id')}"
        items[key] = {'source': 'live screen', 'level': it.get('level'),
                      'text': f"{it.get('title', '')}: {it.get('detail', '')}",
                      'link': it.get('link') or '', 'digest': _digest(it.get('level'), key)}
    for it in ((snapshot.get('paused') or {}).get('items') or []):
        since = _parse(it.get('since'))
        if not since or (now - since).total_seconds() > PAUSED_HOURS * 3600:
            continue
        key = f"paused:{it.get('id')}"
        items[key] = {'source': 'paused task', 'level': 'amber',
                      'text': f"task {it.get('id')} paused since {it.get('since')}: {it.get('name', '')}",
                      'link': it.get('link') or '', 'digest': _digest('paused', key, it.get('since'))}
    for p in promises:
        due = _parse(p.get('until'))
        if p.get('answered') or not due or due > now:
            continue
        key = f"promise:{p['id']}"
        items[key] = {'source': 'promise', 'level': 'amber',
                      'text': f"promised check due {p.get('until')}: {p.get('message', '')}",
                      'link': f"{FACE}/logs/{p['id']}/", 'digest': _digest(key)}
    return items


def read_records(now):
    """The watcher's outcome and promise records over the last two days."""
    start = _iso(now - dt.timedelta(days=2))
    out = {}
    for action in ('watch_outcome', 'watch_promise'):
        got = pn.monitor('epicprod_list_actions', action=action, start_time=start, limit=500)
        out[action] = got.get('items') or []
    return out


def outcomes_by_key(records):
    """The latest outcome per item key: {key: {kind, at, until}}."""
    latest = {}
    for r in records.get('watch_outcome', []):
        key = r.get('subject_key')
        counts = r.get('counts') or {}
        at = _parse(r.get('timestamp'))
        if not key or not at:
            continue
        if key not in latest or at > latest[key]['at']:
            latest[key] = {'kind': r.get('reason') or counts.get('kind'), 'at': at,
                           'until': _parse(counts.get('until')), 'id': r.get('id')}
    return latest


def promises(records, latest):
    out = []
    for r in records.get('watch_promise', []):
        counts = r.get('counts') or {}
        out.append({'id': r.get('id'), 'until': counts.get('until'), 'message': r.get('message', ''),
                    'answered': f"promise:{r.get('id')}" in latest})
    return out


def answered(item, state_item, outcome, now):
    """Whether the item has an outcome that still holds."""
    if not outcome:
        return False
    changed = _parse(state_item.get('changed_at'))
    if changed and outcome['at'] < changed:
        return False
    if outcome['kind'] == 'wait':
        return bool(outcome['until'] and outcome['until'] > now)
    if outcome['kind'] == 'push':
        return (now - outcome['at']).total_seconds() < PUSH_REPEAT_HOURS * 3600
    return outcome['kind'] == 'act'


def post_log(action, message, *, subject_key='', reason='', outcome='ok', live=False,
             sublevel='normal', extra=None):
    base = os.environ.get('SWF_MONITOR_HTTP_URL', '').rstrip('/')
    token = os.environ.get('SWF_API_TOKEN', '')
    if not base or not token:
        raise RuntimeError('SWF_MONITOR_HTTP_URL and SWF_API_TOKEN must be set')
    body = {
        'app_name': 'epicprod', 'instance_name': 'prod-watch', 'timestamp': _iso(_now()),
        'level': 40 if outcome == 'error' else 20, 'levelname': 'ERROR' if outcome == 'error' else 'INFO',
        'message': message, 'module': 'prod_watch', 'funcname': 'tick', 'lineno': 0,
        'process': os.getpid(), 'thread': 0,
        'extra_data': dict({'action': action, 'outcome': outcome, 'sublevel': sublevel,
                            'live_default': live, 'username': 'prod-watch',
                            'subject_type': 'watch_item' if subject_key else '',
                            'subject_key': subject_key, 'reason': reason, 'summary': message},
                           **(extra or {})),
    }
    req = urllib.request.Request(f'{base}/api/logs/', data=json.dumps(body).encode(), method='POST',
                                 headers={'Content-Type': 'application/json',
                                          'Authorization': f'Token {token}'})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b'{}')


def find_session(name):
    """The TJAI session id of the named watcher session on this host, if online."""
    token = os.environ.get('TJAI_MCP_TOKEN', '')
    text = pn._mcp(pn.TJAI_MCP_URL, token, 'list_sessions', {'host': pn.LOCATION})
    data = json.loads(text)
    rows = data.get('sessions', data) if isinstance(data, dict) else data
    for s in rows or []:
        if s.get('name') == name and s.get('online', True):
            return s.get('id')
    return None


def wake(text, session_id):
    """One TJAI message to the watcher session, or to the host's sessions
    when it is offline."""
    token = os.environ.get('TJAI_MCP_TOKEN', '')
    reg = json.loads(pn._mcp(pn.TJAI_MCP_URL, token, 'register_session', dict(
        client='epicprod', name='prod-watch', host=pn.LOCATION, cwd=HERE,
        native_id=str(uuid.uuid4()), model='', resources=[pn.TJAI_RESOURCE])))
    sender = reg.get('id') or reg.get('session_id')
    args = {'sender_id': sender, 'message_id': str(uuid.uuid4()), 'content': text,
            'reply_requested': False}
    if session_id:
        args['recipient_id'] = session_id
    else:
        args['resource'] = pn.TJAI_RESOURCE
    pn._mcp(pn.TJAI_MCP_URL, token, 'send_message', args)


def digest_text(open_items, now, session_online):
    lines = ["Read the current docs/EPICPROD_WATCHER_BRIEF.md before deciding; "
             "its standing authority applies to this wake. It holds no authority "
             "to exclude an OSG glidein site.",
             f"prod-watch {_iso(now)}: {len(open_items)} item(s) need an outcome "
             "(EPICPROD_WATCHER.md, Items and outcomes). For each, record one with "
             "scripts/watch-record.py outcome KEY act|push|wait ...:"]
    if not session_online:
        lines.insert(0, f'The watcher session {SESSION} is offline; this goes to the host\'s sessions.')
    for key, it in sorted(open_items.items(), key=lambda kv: (kv[1]['level'] != 'red', kv[0])):
        lines.append(f"- [{it['level']}] {key} ({it['source']}, open {it['age_min']} min): "
                     f"{it['text'][:300]} {it['link']}")
    return '\n'.join(lines)


def tick(args):
    now = _now()
    try:
        state = json.load(open(args.state))
    except (OSError, ValueError):
        state = {}
    items_state = state.setdefault('items', {})
    snapshot = pn.monitor('swf_comp_snapshot')
    records = read_records(now)
    latest = outcomes_by_key(records)
    items = read_items(snapshot, promises(records, latest), now)

    for key in list(items_state):
        if key in items:
            continue
        gone = _parse(items_state[key].get('gone_at'))
        if gone is None:
            items_state[key]['gone_at'] = _iso(now)
        elif (now - gone).total_seconds() > GONE_HOURS * 3600:
            items_state.pop(key)
    open_items, reasons = {}, {'new': [], 'changed': [], 'reopened': []}
    for key, it in items.items():
        st = items_state.get(key)
        if st is None:
            st = items_state[key] = {'first_seen': _iso(now), 'changed_at': _iso(now),
                                     'digest': it['digest'], 'ticks_open': 0}
            reasons['new'].append(key)
        elif st.get('digest') != it['digest']:
            st.update(digest=it['digest'], changed_at=_iso(now), ticks_open=0, escalated_at=None)
            reasons['changed'].append(key)
        st.pop('gone_at', None)
        if answered(it, st, latest.get(key), now):
            st['ticks_open'] = 0
            st['answered'] = True
            continue
        if st.get('answered'):
            reasons['reopened'].append(key)  # a wait expired or a push came round again
        st['answered'] = False
        st['ticks_open'] = st.get('ticks_open', 0) + 1
        it['age_min'] = int((now - _parse(st['first_seen'])).total_seconds() // 60)
        open_items[key] = it

    session_id = None
    try:
        session_id = find_session(args.session)
    except Exception as exc:
        print(f'session lookup failed: {exc}', file=sys.stderr)
    woke = False
    # A wake carries news: an open item that is new, changed or reopened, or
    # one the last wake did not list. Answering items only shrinks the open
    # set, which wakes no one.
    unlisted = sorted(set(open_items) - set(state.get('last_wake_keys') or []))
    news = {k: [x for x in v if x in open_items] for k, v in reasons.items()}
    news['unlisted'] = [k for k in unlisted if not any(k in v for v in news.values())]
    if open_items and (any(news.values()) or not session_id):
        text = digest_text(open_items, now, bool(session_id))
        if args.dry_run:
            print(text)
        else:
            wake(text, session_id)
        woke = True
        state['last_wake_keys'] = sorted(open_items)

    escalations = []
    for key, it in open_items.items():
        st = items_state[key]
        if st['ticks_open'] >= ESCALATE_TICKS and not st.get('escalated_at'):
            escalations.append(key)
            text = (f"prod-watch escalation{'' if args.escalate else ' (shadow)'}: {key} has had no "
                    f"outcome for {it['age_min']} min ({it['source']}): {it['text'][:400]} {it['link']}"
                    f"; watcher session {'online' if session_id else 'offline'}")
            if args.dry_run:
                print(text)
            else:
                post_log('watch_escalated', text, subject_key=key, outcome='error' if args.escalate else 'ok',
                         live=bool(args.escalate), sublevel='high' if args.escalate else 'normal',
                         extra={'shadow': not args.escalate})
            st['escalated_at'] = _iso(now)

    summary = (f"tick: {len(items)} items, {len(open_items)} without an outcome, "
               f"{'woke ' + (args.session if session_id else 'the host sessions') if woke else 'no wake'}"
               f"{' (' + '; '.join(f'{k}: ' + ', '.join(v) for k, v in news.items() if v) + ')' if woke else ''}, "
               f"{len(escalations)} escalation(s){'' if args.escalate else ' (shadow)'}")
    if args.dry_run:
        print(summary)
        return 0
    post_log('watch_tick', summary, sublevel='low')
    state['last_tick'] = _iso(now)
    tmp = args.state + '.tmp'
    os.makedirs(os.path.dirname(args.state), exist_ok=True)
    with open(tmp, 'w') as fh:
        json.dump(state, fh, indent=1)
    os.replace(tmp, args.state)
    print(summary)
    return 0


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--escalate', action='store_true', help='escalations go live (red, epicprod-live)')
    ap.add_argument('--state', default=STATE_DEFAULT)
    ap.add_argument('--session', default=SESSION)
    return tick(ap.parse_args(argv))


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
