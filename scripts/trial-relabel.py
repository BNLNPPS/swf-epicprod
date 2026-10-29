#!/usr/bin/env python3
"""Relabel PanDA tasks as trials, or back to production, and rebuild the
production metrics over their days.

A production task in which a defect shows that commissioning validation was
incomplete is a trial whatever its PanDA label, and trials never count in
production metrics (Torre Wenaus, 2026-10-29). The relabel adds the task to
the trial list (SysConfig ``compute_efficiency.commissioning_tasks``) with
who, when and why in the action stream, through
``compute_efficiency.relabel_trial``. Every production metric and the Prod
tasks in flight page read that list (``trial_sql``, ``trial_tasks``). The
retained histories are then rebuilt: the efficiency history in full, and
the daily production cores from the earliest relabelled task's start.
PCS trials (``….trialN`` names) are trials by name and need no relabel.

Runs in the deployed monitor's environment on swf-testbed:

    /opt/swf-monitor/current/.venv/bin/python trial-relabel.py 41463 41469 \\
        --by swf-1 --reason "Torre, 2026-10-29: <his words>" --decision-ref "<his turn>"
    # --production takes tasks off the list; --no-rebuild skips the rebuild
"""
import argparse
import json
import os
from pathlib import Path
import sys


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('tasks', nargs='+', type=int, help='JEDI task ids')
    ap.add_argument('--by', required=True)
    ap.add_argument('--reason', required=True)
    ap.add_argument('--decision-ref', required=True)
    ap.add_argument('--production', action='store_true', help='take the tasks off the trial list')
    ap.add_argument('--no-rebuild', action='store_true')
    args = ap.parse_args()
    for raw in Path('/opt/swf-monitor/config/env/production.env').read_text().splitlines():
        raw = raw.strip().removeprefix('export ')
        if raw and not raw.startswith('#') and '=' in raw:
            key, value = raw.split('=', 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    sys.path.insert(0, '/opt/swf-monitor/current/src')
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')
    import django
    django.setup()
    from django.db import connections
    from monitor_app import compute_efficiency, productive_cores
    from monitor_app.compute_efficiency import PANDA_SCHEMA

    with connections['panda'].cursor() as cursor:
        cursor.execute(f'SELECT jeditaskid, starttime, creationdate FROM "{PANDA_SCHEMA}".jedi_tasks'
                       ' WHERE jeditaskid = ANY(%s)', [args.tasks])
        found = {row[0]: (row[1] or row[2]) for row in cursor.fetchall()}
    missing = sorted(set(args.tasks) - set(found))
    if missing:
        sys.exit(f'ERROR: no JEDI task {missing}')
    before, after, record = compute_efficiency.relabel_trial(
        args.tasks, by=args.by, reason=args.reason, decision_ref=args.decision_ref,
        production=args.production)
    out = {'trial_list_before': before, 'trial_list_after': after, 'audit_record': record}
    if record is not None and not args.no_rebuild:
        first_day = min(found.values()).date()
        efficiency = compute_efficiency.product(refresh=True, full=True)
        cores = productive_cores.product(refresh=True, reread_from=first_day)
        if not efficiency.get('value') or not cores.get('value'):
            sys.exit(f'ERROR: rebuild produced no stored result; {json.dumps(out)}')
        out |= {'efficiency_built_at': str(efficiency['built_at']),
                'productive_cores_reread_from': str(first_day)}
    print(json.dumps(out, default=str))


if __name__ == '__main__':
    main()
