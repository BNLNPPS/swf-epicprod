#!/usr/bin/env python3
"""Force-finish a preempted Event Service job (docs/NODE_EVENT_DISPATCHER.md,
Preemption): the job's node is gone, its pilot will never report, and PanDA
would wait out its heartbeat timeout (2 hours by default). From the record
the harness shipped off the node (reports/<PanDA job id>/es.json), this

1. credits the ranges of every close that stood (update_event_ranges,
   finished), which covers any the pilot had not yet passed to the server;
2. ends the job (update_job) with the status given, so the server archives
   it through its fine-grained accounting: finished ranges counted, the
   rest released to the file for the next job, the job finished
   (fg_partial) when any were done.

Both are production-role calls, the role Harvester uses for lost workers.
Run under the PanDA client environment (pclient setup.sh, PANDA_AUTH_VO
with the production role). Dry run unless --apply.

    es_force_finish.py <PanDA job id> [--status finished|failed] [--record es.json] [--apply]
"""
import argparse
import json
import os
import sys

PREEMPTED_CODE = 1256          # the pilot's "job killed: worker preempted / lost" family
CHUNK = 1000


def read_record(pandaid, path=None):
    if path:
        with open(path) as f:
            return json.load(f)
    import boto3
    env = {}
    with open(os.path.expanduser('~/.epic-report-sweeper.env')) as f:
        for line in f:
            k, sep, v = line.strip().removeprefix('export ').partition('=')
            if sep:
                env[k.strip()] = v.strip().strip('"').strip("'")
    s3 = boto3.client('s3', region_name=env.get('REPORT_SWEEP_REGION') or 'us-east-1',
                      aws_access_key_id=env['REPORT_SWEEP_ACCESS_KEY_ID'],
                      aws_secret_access_key=env['REPORT_SWEEP_SECRET_ACCESS_KEY'])
    prefix = (env.get('REPORT_SWEEP_PREFIX') or 'reports/').strip('/') + '/'
    body = s3.get_object(Bucket=env['REPORT_SWEEP_BUCKET'], Key=f'{prefix}{pandaid}/es.json')['Body'].read()
    return json.loads(body)


def closed_range_ids(record):
    """The range ids of the units in closes that stood. A unit's first range
    id is <task>-<job>-<file>-<event>-<attempt>; its ranges are one event
    each over [startEvent, lastEvent]."""
    ids = []
    for u in record.get('done') or []:
        rng, uid = u.get('range') or {}, u.get('unit_id') or ''
        parts = uid.split('-')
        if len(parts) < 5 or rng.get('startEvent') is None:
            continue
        prefix, attempt = '-'.join(parts[:3]), parts[4]
        ids += [f'{prefix}-{e}-{attempt}' for e in range(int(rng['startEvent']), int(rng['lastEvent']) + 1)]
    return ids


def api(endpoint, data):
    import pandaclient.Client as C
    curl = C._Curl()
    curl.sslCert = C._x509()
    curl.sslKey = C._x509()
    return curl.post(f'{C.server_base_path_ssl}/{endpoint}', data, json_out=True)


def job_state(pandaid):
    """The job's status and attempt number from the server. The final update
    must carry the attempt: without it the output report is filed under
    attempt 0, the adder drops it as the wrong attempt (adder_gen.py,
    process_job_report) and the job stays holding, and a second final update
    is ignored as already done (job_complex_module.py, updateJobStatus)."""
    import pandaclient.Client as C
    status, jobs = C.getJobStatus([pandaid])
    if status != 0 or not jobs or jobs[0] is None:
        raise SystemExit(f'job {pandaid}: getJobStatus failed (status {status})')
    return jobs[0].jobStatus, int(jobs[0].attemptNr)


def count_true(rets):
    """update_event_ranges returns its per-range results as the text of a
    Python list ("[True, True, ...]"), not a JSON list."""
    if isinstance(rets, str):
        return rets.count('True'), rets.count('True') + rets.count('False')
    return sum(1 for x in rets if x is True), len(rets)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('pandaid', type=int)
    ap.add_argument('--status', default='finished', choices=('finished', 'failed'))
    ap.add_argument('--record')
    ap.add_argument('--attempt', type=int, help="the attempt expected; read from the server, refused on mismatch")
    ap.add_argument('--apply', action='store_true')
    args = ap.parse_args()

    record = read_record(args.pandaid, args.record)
    ids = closed_range_ids(record)
    print(f'job {args.pandaid}: record written {record.get("written_at")}, '
          f'{len(record.get("done") or [])} units in closes that stood ({len(ids)} ranges), '
          f'{len(record.get("in_flight") or [])} in flight, '
          f'{len(record.get("awaiting_close") or [])} awaiting a close')
    if not args.apply:
        print('dry run: nothing sent')
        return 0

    job_status, attempt = job_state(args.pandaid)
    if job_status not in ('running', 'starting', 'stagein', 'stageout'):
        print(f'job {args.pandaid} is {job_status}: a final update would be ignored; nothing sent')
        return 1
    if args.attempt is not None and args.attempt != attempt:
        print(f'job {args.pandaid} is at attempt {attempt}, not {args.attempt}; nothing sent')
        return 1

    for i in range(0, len(ids), CHUNK):
        batch = [{'eventRangeID': r, 'eventStatus': 'finished'} for r in ids[i:i + CHUNK]]
        status, out = api('event/update_event_ranges', {'event_ranges': json.dumps(batch), 'version': 0})
        ok = isinstance(out, dict) and out.get('success')
        rets = (((out or {}).get('data') or {}).get('Returns') or []) if isinstance(out, dict) else []
        n_true, n_all = count_true(rets)
        print(f'update_event_ranges {i}-{i + len(batch)}: http {status}, success {ok}, {n_true} true of {n_all}'
              + ('' if ok else f', message {(out or {}).get("message") if isinstance(out, dict) else out}'))
    data = {'job_id': args.pandaid, 'job_status': args.status, 'attempt_nr': attempt,
            'pilot_error_code': PREEMPTED_CODE,
            'pilot_error_diag': 'Event Service node lost (preempted); force-finished from the record '
                                'the harness shipped: every close that stood credited'}
    status, out = api('pilot/update_job', data)
    print(f'update_job {args.status}: http {status}, response {json.dumps(out)[:400]}')
    return 0 if isinstance(out, dict) and out.get('success') else 1


if __name__ == '__main__':
    sys.exit(main())
