#!/usr/bin/env python3
"""Run the files JEDI left out of an EVGEN Event Service task as tasks of
their own (swf_epicprod/evgen_es.py, tails; docs/NERSC_PERLMUTTER.md, EVGEN
input in production): JEDI takes a task's files at one count each and drops a
last file the task's total takes only part way, so its covered events run as a
one-file task of the same PCS task, on the queue the task is placed on. The
setup the task was submitted with decides which files were left out.

Run in the monitor Django environment with the testbed venv and ~/.env:

    evgen-es-tail.py <composed task name> --by swf-1 --reason TEXT [--dry-run]

--dry-run prints each tail's setup and PanDA spec; submits nothing.
"""
import argparse
import json
import os

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django
django.setup()

from types import SimpleNamespace

from pcs import services
from pcs.commands import build_evgen_task_params
from pcs.models import ProdTask
from swf_epicprod import evgen_es

SPEC_KEYS = ('inputDataset', 'inputFiles', 'nFilesPerJob', 'nEventsPerInputFile', 'nEventsPerJob',
             'nJobs', 'nEvents', 'fineGrainedRangeEvents')


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('task', help="the task's composed name")
    parser.add_argument('--by', required=True)
    parser.add_argument('--reason', required=True)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    task = ProdTask.objects.get(name=args.task)
    if task.panda_task_id is None:
        raise SystemExit(f'{args.task} is not submitted; its feed submits its tails with it')
    shapes = evgen_es.tails(task)
    if not shapes:
        print(f'{args.task}: JEDI takes every file; no tail')
        return
    for shape in shapes:
        if args.dry_run:
            spec = build_evgen_task_params(task, panda_tasks=SimpleNamespace(metadata={'event_service': shape}))
            print(json.dumps({k: spec.get(k) for k in SPEC_KEYS}, indent=2, default=str))
            continue
        attempt = services.prodtask_submit_tail(task=task, event_service=shape, changed_by=args.by)
        print(f'{args.task}: tail {shape["input_files"][0]} ({shape["events"]:,} events) queued as '
              f'{attempt.task_name}; reason: {args.reason}')


if __name__ == '__main__':
    main()
