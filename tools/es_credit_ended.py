#!/usr/bin/env python3
"""Credit the closes that stood of an Event Service job that ended without
reporting them (docs/NODE_EVENT_DISPATCHER.md, Preemption).

On Perlmutter the harvester ends a job whose worker is gone within minutes
(taskbuffer 300, "The worker was finished while the job was running"), before
the close-out (swf_epicprod/es_closeout.py) can credit it; the close-out acts
only on running jobs. The server's fine-grained accounting then counts the
ranges the pilot had reported (status finished, bound to the job) and releases
every other range of the job to the file (status ready, bound to the jobset),
including ranges whose output stands registered in Rucio, which the next job
would simulate again (job 6087270, task 41316, 2026-10-26: 249,480 events in
seven closes, 122,012 reported, 127,468 released).

This puts each released range of a close that stood back where the pilot's
report would have left it: status finished, bound to the job, as the server's
own update does (task_event_module.updateEventRanges). A range in any other
state is left alone and named. The ranges come from the record the harness
shipped (closed_range_ids, as the close-out reads them). One transaction;
the row count must match or nothing is written. Dry run unless --apply.

Runs in the deployed monitor's environment on swf-testbed, on an operator's
word, with the job's task paused so no new job takes the released ranges:

    /opt/swf-monitor/current/.venv/bin/python es_credit_ended.py <PanDA job id> [--apply]
"""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

ENDED = ('finished', 'failed', 'closed', 'cancelled')
ST_READY, ST_FINISHED = 0, 3


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


def read_record(pandaid, path=None):
    if path:
        return json.loads(Path(path).read_text())
    from monitor_app.payload_reports import sweeper_client
    store = sweeper_client()
    if store is None:
        sys.exit('ERROR: the report store credential is unusable')
    client, bucket, prefix = store
    return json.loads(client.get_object(Bucket=bucket, Key=f'{prefix}{pandaid}/es.json')['Body'].read())


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('pandaid', type=int)
    ap.add_argument('--record', help='the shipped record as a file, instead of the store')
    ap.add_argument('--requested-by', default='wenaus')
    ap.add_argument('--apply', action='store_true')
    args = ap.parse_args()

    deployed_environment()
    from django.db import connections, transaction
    from monitor_app.epicprod_logging import log_epicprod_action
    from monitor_app.panda.constants import PANDA_SCHEMA
    from swf_epicprod.es_closeout import closed_range_ids

    with connections['panda'].cursor() as cursor:
        cursor.execute(f'SELECT jobstatus, jobsubstatus, jeditaskid, jobsetid, nevents FROM "{PANDA_SCHEMA}".jobsarchived4 '
                       'WHERE pandaid = %s', [args.pandaid])
        row = cursor.fetchone()
    if row is None:
        sys.exit(f'ERROR: job {args.pandaid} is not in the archive; a running job is credited by the close-out')
    status, substatus, task, jobset, nevents = row
    if status not in ENDED:
        sys.exit(f'ERROR: job {args.pandaid} is {status}')

    record = read_record(args.pandaid, args.record)
    if str(record.get('pandaid')) != str(args.pandaid):
        sys.exit(f'ERROR: the record is of job {record.get("pandaid")}')
    keys = []
    for rid in closed_range_ids(record):
        t, job, fileid, process, attempt = (int(x) for x in rid.split('-'))
        if t != task or job != args.pandaid:
            sys.exit(f'ERROR: range {rid} is not of task {task} job {args.pandaid}')
        keys.append((fileid, process, attempt))
    saved = sum(int(c.get('events') or 0) for c in record.get('closes') or [] if c.get('ok'))

    files, processes, attempts = (list(x) for x in zip(*keys)) if keys else ([], [], [])
    match = (f'FROM "{PANDA_SCHEMA}".jedi_events e '
             'JOIN unnest(%s::bigint[], %s::bigint[], %s::bigint[]) AS k(fileid, process, attempt) '
             'ON e.fileid = k.fileid AND e.job_processid = k.process AND e.attemptnr = k.attempt '
             'WHERE e.jeditaskid = %s')
    with connections['panda'].cursor() as cursor:
        cursor.execute(f'SELECT e.pandaid, e.status, count(*) {match} GROUP BY 1, 2 ORDER BY 1, 2',
                       [files, processes, attempts, task])
        states = cursor.fetchall()
    credited = sum(n for p, s, n in states if p == args.pandaid and s == ST_FINISHED)
    released = sum(n for p, s, n in states if p == jobset and s == ST_READY)
    other = [(p, s, n) for p, s, n in states
             if not ((p == args.pandaid and s == ST_FINISHED) or (p == jobset and s == ST_READY))]
    found = sum(n for _, _, n in states)
    summary = {'pandaid': args.pandaid, 'jeditaskid': task, 'job': f'{status}/{substatus}', 'job_nevents': nevents,
               'saved_in_closes': saved, 'stood_ranges': len(keys), 'found': found,
               'already_credited': credited, 'released_to_credit': released, 'other_states': other}
    print(json.dumps(summary))
    if found != len(keys):
        sys.exit(f'ERROR: {len(keys) - found} ranges of closes that stood have no row')
    if other:
        sys.exit('ERROR: ranges in other states; nothing written')
    if not args.apply or not released:
        return

    with transaction.atomic(using='panda'), connections['panda'].cursor() as cursor:
        cursor.execute(f'UPDATE "{PANDA_SCHEMA}".jedi_events e SET status = %s, pandaid = %s '
                       'FROM unnest(%s::bigint[], %s::bigint[], %s::bigint[]) AS k(fileid, process, attempt) '
                       'WHERE e.jeditaskid = %s AND e.pandaid = %s AND e.status = %s '
                       'AND e.fileid = k.fileid AND e.job_processid = k.process AND e.attemptnr = k.attempt',
                       [ST_FINISHED, args.pandaid, files, processes, attempts, task, jobset, ST_READY])
        if cursor.rowcount != released:
            raise RuntimeError(f'updated {cursor.rowcount} rows, expected {released}; rolled back')
    print(json.dumps({'pandaid': args.pandaid, 'credited': released, 'now_credited': credited + released}))
    log_epicprod_action(
        'ops-agent', 'es_credit_ended', subject_type='panda_job', subject_key=str(args.pandaid),
        username=args.requested_by, outcome='ok', sublevel='normal', live_default=False,
        message=(f'Event Service job {args.pandaid} (task {task}) ended {status}/{substatus} without reporting '
                 f'{released} ranges of closes that stood; set finished on the job, '
                 f'{credited + released} of {len(keys)} now credited'),
        jeditaskid=task, ranges=released, credited=credited + released, saved=saved)


if __name__ == '__main__':
    main()
