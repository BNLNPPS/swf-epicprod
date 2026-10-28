#!/usr/bin/env python3
"""Submit a PCS task over a registered EVGEN sample to Perlmutter through the
Event Service, or a new attempt of one already submitted (swf_epicprod/evgen_es.py;
docs/NERSC_PERLMUTTER.md, EVGEN input in production): its input read from the
catalog that holds it, the whole task staged at NERSC, its ``event_service``
override built afresh in the current production shape, then placed on
NERSC_Perlmutter_epic_es and the submission queued through the operations
agent, as every PCS submission goes. Nothing of an earlier attempt is reused: a
new attempt is a new PanDA task (``.tryN``) writing in its own namespace.

Run in the monitor Django environment with the testbed venv and ~/.env:

    evgen-es-submit.py <composed task name> --by swf-1 --reason TEXT [--rerun] [--wait] [--dry-run]

--rerun   a new attempt of a task already submitted (Rerun Entire Task)
--wait    wait for the NERSC copy, else exit while it is made (run again after)
--dry-run stage, then print the shape and the PanDA spec; submit nothing
"""
import argparse
import json
import os
import time

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django
django.setup()

from pcs import services
from pcs.commands import build_evgen_task_params
from pcs.models import ProdTask
from swf_epicprod import evgen_es, nersc_stage
from swf_epicprod.gke_pilots import settings as gke_settings

QUEUE = 'NERSC_Perlmutter_epic_es'
SPEC_KEYS = ('taskName', 'outDS', 'containerImage', 'inputDataset', 'nFilesPerJob', 'nEventsPerInputFile',
             'nEventsPerJob', 'nJobs', 'nEvents', 'nEventsPerWorker', 'fineGrainedRangeEvents',
             'directInput', 'nCore', 'memory', 'walltimeHours', 'exec')


def prepared(task, proxy, wait):
    """The task with its input at NERSC and its shape set; None while the
    copy is under way and ``wait`` is not asked."""
    while True:
        try:
            evgen_es.prepare(task, proxy, site=QUEUE)
            return task
        except evgen_es.InputStaging as staging:
            print(f'{task.name}: {staging}')
            if not wait:
                return None
            job = (task.overrides or {}).get('site_input') or {}
            if job.get('job_id'):
                nersc_stage.wait_job(nersc_stage.Client(), job['job_id'], timeout_s=3600)
            else:
                time.sleep(30)
            task.refresh_from_db()


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('task', help="the task's composed name")
    parser.add_argument('--by', required=True)
    parser.add_argument('--reason', required=True)
    parser.add_argument('--rerun', action='store_true')
    parser.add_argument('--wait', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    task = ProdTask.objects.get(name=args.task)
    if task.panda_task_id is not None and not args.rerun:
        raise SystemExit(f'{args.task} is already submitted as jediTaskID {task.panda_task_id}; '
                         '--rerun makes a new attempt')
    if task.panda_task_id is None and args.rerun:
        raise SystemExit(f'{args.task} has no PanDA submission to rerun')
    if prepared(task, gke_settings()['proxy'], args.wait) is None:
        raise SystemExit(2)
    shape = task.overrides['event_service']
    if args.dry_run:
        spec = build_evgen_task_params(task)
        print(json.dumps({'event_service': shape,
                          'spec': {k: spec.get(k) for k in SPEC_KEYS}}, indent=2, default=str))
        return
    placement = {'recommended': QUEUE, 'reason': args.reason}
    if args.rerun:
        previous = task.panda_task_id
        services.prodtask_rerun_entire_task_request(task=task, site=QUEUE, placement=placement,
                                                    changed_by=args.by)
        what = f'new attempt (after jediTaskID {previous})'
    else:
        services.prodtask_submit_request(task=task, changed_by=args.by, source='perlmutter_evgen_es',
                                         site=QUEUE, placement=placement)
        what = 'submission'
    print(f'{args.task}: {what} queued on {QUEUE}: {shape["events"]:,} events over '
          f'{shape["files"]} files ({shape["input_dataset"]})')


if __name__ == '__main__':
    main()
