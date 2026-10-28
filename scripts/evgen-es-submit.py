#!/usr/bin/env python3
"""Submit a PCS task over a registered EVGEN sample to Perlmutter through the
Event Service (swf_epicprod/evgen_es.py; docs/NERSC_PERLMUTTER.md, EVGEN input
in production): stage its covered files at BNL, set its event_service
override, place it on NERSC_Perlmutter_epic_es and queue the submission
through the operations agent, as every PCS submission goes.

Run in the monitor Django environment with the testbed venv and ~/.env:

    evgen-es-submit.py <composed task name> --by swf-1 --reason TEXT [--dry-run]

--dry-run builds and prints the PanDA spec and submits nothing.
"""
import argparse
import json
import os

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django
django.setup()

from pcs import services
from pcs.commands import build_evgen_task_params
from pcs.models import ProdTask
from swf_epicprod import evgen_es
from swf_epicprod.gke_pilots import settings as gke_settings

QUEUE = 'NERSC_Perlmutter_epic_es'
SPEC_KEYS = ('taskName', 'outDS', 'containerImage', 'inputDataset', 'nFilesPerJob', 'nEventsPerInputFile',
             'nEventsPerJob', 'nJobs', 'nEvents', 'nEventsPerWorker', 'fineGrainedRangeEvents',
             'directInput', 'nCore', 'memory', 'walltimeHours', 'exec')


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('task', help="the task's composed name")
    parser.add_argument('--by', required=True)
    parser.add_argument('--reason', required=True)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    task = ProdTask.objects.get(name=args.task)
    if task.panda_task_id is not None:
        raise SystemExit(f'{args.task} is already submitted as jediTaskID {task.panda_task_id}')
    staged = evgen_es.stage(task, gke_settings()['proxy'])
    shape = evgen_es.event_service(task, staged)
    overrides = dict(task.overrides or {}, event_service=shape)
    if args.dry_run:
        task.overrides = overrides  # in memory only
        spec = build_evgen_task_params(task)
        print(json.dumps({'event_service': shape,
                          'spec': {k: spec.get(k) for k in SPEC_KEYS}}, indent=2, default=str))
        return
    task.overrides = overrides
    task.save(update_fields=['overrides'])
    services.prodtask_submit_request(task=task, changed_by=args.by, source='perlmutter_evgen_es',
                                     site=QUEUE, placement={'recommended': QUEUE, 'reason': args.reason})
    print(f'{args.task}: submission queued on {QUEUE}: {shape["events"]:,} events over '
          f'{shape["files"]} files ({staged["dataset"]})')


if __name__ == '__main__':
    main()
