#!/usr/bin/env python3
"""Run the files JEDI left out of an EVGEN Event Service task as tasks of
their own (swf_epicprod/evgen_es.py, tails; docs/NERSC_PERLMUTTER.md, EVGEN
input in production): JEDI takes a task's files at one count each and drops a
last file the task's total takes only part way, so its covered events run as a
one-file task of the same PCS task, on the queue the task is placed on. The
setup the task was submitted with decides which files were left out.

Run in the monitor Django environment with the testbed venv and ~/.env:

    evgen-es-tail.py <composed task name> --by swf-1 --reason TEXT [--fresh] [--dry-run]
    evgen-es-tail.py <composed task name> --resend ATTEMPT --by swf-1 --reason TEXT

--dry-run prints each tail's setup and PanDA spec; submits nothing.
--fresh reads the tails against the setup built afresh from the catalog, for a
task whose own setup predates the catalog input and names no files (41525 and
41526, p4495 and p4497 .try2); the fresh setup must take the files the task's
setup took.
--resend queues again a tail attempt (its PCS PandaTasks id) whose submission
never reached PanDA.
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


def fresh_setup(task):
    """The task's setup built afresh from its catalog, with the files it read:
    ``(shape, staged)``. JEDI takes the leading files at one count each up to
    the total, so the same count per file, total and file count mean the
    same files taken; anything else refuses, since a tail could then repeat
    a file the task ran."""
    from pcs.commands import pinned_site
    ran = (task.overrides or {}).get('event_service') or {}
    staged = evgen_es.catalog_input(task)
    shape = evgen_es.event_service(task, staged, evgen_es.site_shape(pinned_site(task)))
    for key in ('events_per_file', 'events', 'files'):
        if shape.get(key) != ran.get(key):
            raise SystemExit(f'{task.name}: the fresh setup has {key} {shape.get(key)}, '
                             f'the task ran with {ran.get(key)}; no tail')
    return shape, staged


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('task', help="the task's composed name")
    parser.add_argument('--by', required=True)
    parser.add_argument('--reason', required=True)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--fresh', action='store_true')
    parser.add_argument('--resend', type=int, metavar='ATTEMPT')
    args = parser.parse_args()
    task = ProdTask.objects.get(name=args.task)
    if args.resend:
        attempt = services.prodtask_resend_tail(
            task=task, panda_tasks=task.panda_tasks.get(pk=args.resend))
        print(f'{args.task}: tail {attempt.task_name} queued again; reason: {args.reason}')
        return
    if task.panda_task_id is None:
        raise SystemExit(f'{args.task} is not submitted; its feed submits its tails with it')
    shapes = evgen_es.tails(task, *fresh_setup(task)) if args.fresh else evgen_es.tails(task)
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
