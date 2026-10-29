#!/usr/bin/env python3
"""List, create, fulfil or replace pings, the alarm system's dated
obligations (swf-monitor docs/PINGS.md), through the executors the alarm
dashboard's controls use (monitor_app/alarms_data.py). Days left are
computed from today's Eastern date at each run.

Runs in the deployed monitor's environment on swf-testbed:

    PY=/opt/swf-monitor/current/.venv/bin/python
    $PY pings.py list [--all]
    $PY pings.py create --title 'Renew the osgsub01 host certificate' --due 2026-11-20 \\
        --owner @prodops --note 'what to do and where' [--url URL] [--lead-days 7]
    $PY pings.py fulfil PING_ID
    $PY pings.py replace PING_ID [--title ...] [--due ...] [--owner ...] [--note ...]

A ping cannot be edited in place: replace creates the new ping from the old
one's fields with the changes given, then marks the old one fulfilled.
"""
import argparse
import json
import os
from pathlib import Path
import sys


def setup():
    for raw in Path('/opt/swf-monitor/config/env/production.env').read_text().splitlines():
        raw = raw.strip().removeprefix('export ')
        if raw and not raw.startswith('#') and '=' in raw:
            key, value = raw.split('=', 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    sys.path.insert(0, '/opt/swf-monitor/current/src')
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')
    import django
    django.setup()


def row(p):
    return {k: p[k] for k in ('id', 'title', 'due', 'days_left', 'state', 'lead_days',
                              'owner', 'origin', 'created_by', 'fulfilled_by')} | {
        'fulfilled_at': p['fulfilled_at'].isoformat() if p['fulfilled_at'] else None}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='command', required=True)
    ls = sub.add_parser('list', help='open pings by due date')
    ls.add_argument('--all', action='store_true', help='include fulfilled pings')
    for name in ('create', 'replace'):
        p = sub.add_parser(name)
        if name == 'replace':
            p.add_argument('ping')
        p.add_argument('--title', required=(name == 'create'))
        p.add_argument('--due', required=(name == 'create'), help='YYYY-MM-DD')
        p.add_argument('--owner')
        p.add_argument('--note')
        p.add_argument('--url')
        p.add_argument('--lead-days', type=int)
        p.add_argument('--by', default='wenaus')
    ff = sub.add_parser('fulfil')
    ff.add_argument('ping')
    ff.add_argument('--by', default='wenaus')
    args = ap.parse_args()

    setup()
    from monitor_app import alarms_data
    try:
        if args.command == 'list':
            open_rows, done_rows = alarms_data.list_pings()
            out = {'open': [row(p) for p in open_rows]}
            if args.all:
                out['fulfilled'] = [row(p) for p in done_rows]
        elif args.command == 'fulfil':
            e, log_id = alarms_data.ping_fulfil_execute(args.ping, changed_by=args.by)
            out = {'fulfilled': e.id, 'title': e.title, 'by': args.by, 'log_id': log_id}
        else:
            base = {}
            if args.command == 'replace':
                old = alarms_data.get_ping(args.ping)
                if old is None:
                    raise alarms_data.PingError('no such ping')
                if (old.data or {}).get('status') == 'fulfilled':
                    raise alarms_data.PingError('already fulfilled')
                d = old.data or {}
                base = {'title': old.title, 'due': d.get('due'), 'owner': d.get('owner'),
                        'note': old.content, 'url': d.get('url'), 'lead_days': d.get('lead_days')}
            given = {'title': args.title, 'due': args.due, 'owner': args.owner,
                     'note': args.note, 'url': args.url, 'lead_days': args.lead_days}
            payload = base | {k: v for k, v in given.items() if v is not None}
            e, log_id = alarms_data.ping_create_execute(payload, changed_by=args.by)
            out = {'created': e.id, 'title': e.title, 'due': e.data.get('due'),
                   'owner': e.data.get('owner'), 'by': args.by, 'log_id': log_id}
            if args.command == 'replace':
                old_e, old_log = alarms_data.ping_fulfil_execute(args.ping, changed_by=args.by)
                out |= {'replaced': old_e.id, 'replaced_log_id': old_log}
    except alarms_data.PingError as exc:
        sys.exit(f'ERROR: {exc}')
    print(json.dumps(out, indent=1, default=str))


if __name__ == '__main__':
    main()
