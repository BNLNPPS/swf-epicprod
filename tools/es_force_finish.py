#!/usr/bin/env python3
"""Force-finish a preempted Event Service job by hand (docs/NODE_EVENT_DISPATCHER.md,
Preemption). The production-operations agent does this unattended on the
queues listed in ``es_closeout.queues`` (swf_epicprod/es_closeout.py, which
holds the logic); this is the same close-out for one job, on an operator's
word: credit the ranges of every close that stood in the record the harness
shipped, then send the final update with the job's attempt number.

Run under the PanDA client environment (pclient setup.sh, PANDA_AUTH_VO
with the production role). The record is read from the store with the
sweeper credential (needs boto3), or given with --record. Dry run unless
--apply.

    es_force_finish.py <PanDA job id> [--status finished|failed] [--record es.json] [--apply]
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from swf_epicprod.es_closeout import closed_range_ids, finish_job  # noqa: E402


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
    result = finish_job(args.pandaid, ids, status=args.status, expect_attempt=args.attempt,
                        apply=args.apply)
    if result.get('refused'):
        print(f'job {args.pandaid}: {result["refused"]}; nothing sent')
        return 1
    if not args.apply:
        print(f'job {args.pandaid} is {result["job_status"]} at {result["queue"]}, attempt '
              f'{result["attempt"]}; dry run: nothing sent')
        return 0
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
