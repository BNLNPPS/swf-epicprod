#!/usr/bin/env python3
"""End a PanDA queue's harvester workers through PanDA: the server's
harvester sweep (POST /api/v1/harvester/add_sweep_command, production role)
queues a KILL_WORKERS command for the queue's harvester, whose sweeper
cancels the batch jobs as the harvester's own user. Use it when a queue's
nodes must stop at once: a killed PanDA job's pilot hears of the kill only at
its next heartbeat (30 minutes), and an Event Service harness works on from
its prefetched ranges until then (task 41479, 2026-10-28).

Run under the PanDA client environment with the production role:

    source ~/pclient/run/setup.sh; export PANDA_AUTH_VO=EIC.production
    python3 harvester_sweep.py NERSC_Perlmutter_epic_es [--status running,submitted] [--apply]

Dry run unless --apply. The server refuses a queue whose PanDA record names
no harvester ("Queue not served by any harvester ID"), as it did for
NERSC_Perlmutter_epic_es on 2026-10-28.
"""
import argparse
import json
import sys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('queue')
    ap.add_argument('--status', default='submitted,running',
                    help='worker statuses to end, comma-separated')
    ap.add_argument('--apply', action='store_true')
    args = ap.parse_args()
    statuses = [s for s in args.status.split(',') if s]
    request = {'panda_queue': args.queue, 'status_list': statuses,
               'ce_list': [], 'submission_host_list': []}
    if not args.apply:
        print(json.dumps({'dry_run': True, 'request': request}))
        return 0

    from pandaclient import Client

    @Client.curl_request_decorator(endpoint='harvester/add_sweep_command', method='post',
                                   json_out=True, output_mode='extended')
    def add_sweep_command(panda_queue, status_list, ce_list, submission_host_list, verbose=False):
        return {'panda_queue': panda_queue, 'status_list': status_list,
                'ce_list': ce_list, 'submission_host_list': submission_host_list}

    status, output = add_sweep_command(**request)
    print(json.dumps({'request': request, 'transport_status': status, 'answer': output}, default=str))
    ok = status == 0 and isinstance(output, dict) and output.get('success') is True
    if not ok:
        print('ERROR: the server did not accept the sweep', file=sys.stderr)
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
