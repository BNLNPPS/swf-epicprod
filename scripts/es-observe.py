#!/usr/bin/env python3
"""Read an Event Service task's jobs and their shipped harness records
(docs/JOB_REPORTING.md, The Event Service record), read-only.

Each observation prints one line per job: status, record age, units in flight,
done and failed, events saved in closes that stood, the last close, node
memory available, the units' paths (resident or run.sh), distinct simulation
processes, and the shared reconstruction pool's waits and member memory
(EPICPROD_PAYLOAD.md, Shared resident reconstruction). Times are Eastern.
With --every the observation repeats until every job has ended; with
--evidence each observation's records are kept there.

Runs in the deployed monitor's environment on swf-testbed:

    /opt/swf-monitor/current/.venv/bin/python es-observe.py --task 41249 [--every 120] [--evidence DIR]
"""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import statistics
import sys
import time
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo('America/New_York')
ENDED = {'finished', 'failed', 'cancelled', 'closed'}


def deployed_environment():
    for raw in Path('/opt/swf-monitor/config/env/production.env').read_text().splitlines():
        raw = raw.strip().removeprefix('export ')
        if raw and not raw.startswith('#') and '=' in raw:
            key, value = raw.split('=', 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    sys.path.insert(0, '/opt/swf-monitor/current/src')
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')
    import django
    django.setup()


def eastern(epoch):
    return datetime.fromtimestamp(epoch, EASTERN).strftime('%H:%M:%S') if epoch else '-'


def summary(job, record, now):
    started = record.get('started_at') or now
    done, failed = record.get('done', []), record.get('failed', [])
    closes = record.get('closes', [])
    stood = [c for c in closes if c.get('ok')]
    units = done + record.get('in_flight', [])
    paths = {}
    for unit in done:
        paths[unit.get('unit_path') or '?'] = paths.get(unit.get('unit_path') or '?', 0) + 1
    waits = [u['reconstruction']['pool_wait_s'] for u in done
             if (u.get('reconstruction') or {}).get('pool_wait_s') is not None]
    member_kb = [((u.get('reconstruction') or {}).get('memory') or {}).get('private_kb') for u in done]
    member_kb = [kb for kb in member_kb if kb]
    last = closes[-1] if closes else {}
    return {
        'pandaid': job['pandaid'], 'status': job.get('jobstatus'),
        'elapsed_s': round(record.get('written_at', now) - started) if record else None,
        'record_age_s': round(now - record['written_at']) if record.get('written_at') else None,
        'slots': record.get('slots'), 'in_flight': len(record.get('in_flight', [])),
        'queued': record.get('queued_units'), 'done_units': len(done), 'failed_units': len(failed),
        'events_saved': sum(c.get('events') or 0 for c in stood), 'closes_stood': len(stood),
        'closes_failed': len([c for c in closes if c.get('ok') is False]),
        'last_close': {k: last.get(k) for k in ('index', 'events', 'ok', 'stage', 'merged_bytes')} if last else None,
        'last_close_wall_s': round(last['ended_at'] - last['started_at']) if last.get('ended_at') else None,
        'awaiting_close': len(record.get('awaiting_close', [])),
        'mem_available_gb': round((record.get('mem_available_kb') or 0) / 1e6, 1),
        'loadavg': record.get('loadavg'), 'taking': record.get('taking'),
        'ended_at': eastern(record.get('ended_at')),
        'unit_paths': paths,
        'simulation_pids': len({u['simulation']['pid'] for u in units if (u.get('simulation') or {}).get('pid')}),
        'pool_wait_s': {'median': round(statistics.median(waits), 1), 'max': round(max(waits), 1)} if waits else None,
        'pool_member_private_gb_max': round(max(member_kb) / 1e6, 2) if member_kb else None,
        'first_failure': (failed[0].get('message') or '')[:300] if failed else None,
    }


def observe(task, evidence):
    from monitor_app.panda.queries import list_jobs
    from monitor_app.payload_reports import sweeper_client
    store = sweeper_client()
    if not store:
        raise RuntimeError('the report store is not configured')
    client, bucket, prefix = store
    now = time.time()
    jobs = list_jobs(taskid=task, days=7, limit=50).get('jobs', [])
    lines = []
    for job in jobs:
        try:
            body = client.get_object(Bucket=bucket, Key=f"{prefix}{job['pandaid']}/es.json")['Body'].read()
            record = json.loads(body)
        except client.exceptions.NoSuchKey:
            record = {}
        if evidence and record:
            (evidence / f"{job['pandaid']}-record.json").write_text(json.dumps(record, indent=2) + '\n')
        lines.append(summary(job, record, now))
    return jobs, lines


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--task', type=int, required=True)
    ap.add_argument('--every', type=int, default=0, help='seconds between observations, until every job ends')
    ap.add_argument('--evidence', type=Path, help='keep each job record here')
    args = ap.parse_args()
    deployed_environment()
    if args.evidence:
        args.evidence.mkdir(parents=True, exist_ok=True)
    while True:
        jobs, lines = observe(args.task, args.evidence)
        stamp = eastern(time.time())
        if not lines:
            print(json.dumps({'at': stamp, 'task': args.task, 'jobs': 0}), flush=True)
        for line in lines:
            print(json.dumps(dict(at=stamp, **line), default=str), flush=True)
        ended = jobs and all(j.get('jobstatus') in ENDED for j in jobs)
        if not args.every or ended:
            return 0
        time.sleep(args.every)


if __name__ == '__main__':
    sys.exit(main())
