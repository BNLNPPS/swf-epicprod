#!/usr/bin/env python3
"""Read the events every earlier attempt of an EVGEN Event Service task left
unread, one new attempt over each unread segment of each file
(swf_epicprod/evgen_es.py, recovery; docs/NERSC_PERLMUTTER.md, EVGEN input in
production). The segments come from PanDA's credited ranges over every attempt
of the PCS task, against each file's catalog event count. A segment already
carried by an attempt is not submitted again.

Run in the monitor Django environment with the testbed venv and ~/.env:

    evgen-es-recovery.py <composed task name> [--dry-run]
    evgen-es-recovery.py <composed task name> --by swf-1 --reason TEXT [--only LFN:OFFSET] [--limit N]

--dry-run   lists each segment and its PanDA spec; submits nothing (the default without --by)
--only      submits the one segment of that file at that zero-based offset
--limit     submits at most N segments
"""
import argparse
import json
import os

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django
django.setup()

from types import SimpleNamespace

from django.db import connections

from pcs import services
from pcs.commands import build_evgen_task_params
from pcs.models import ProdTask
from swf_epicprod import evgen_es

SPEC_KEYS = ('inputDataset', 'inputFiles', 'nFilesPerJob', 'nEventsPerInputFile', 'nEventsPerJob',
             'nJobs', 'nEvents', 'fineGrainedRangeEvents')
CREDITED = (3, 6)    # JEDI_Events finished, done


def attempts(task):
    """The JEDI task ids of every attempt of the PCS task."""
    ids = {int(task.panda_task_id)} if task.panda_task_id else set()
    ids.update(int(i) for i in task.panda_tasks.filter(jedi_task_id__isnull=False)
               .values_list('jedi_task_id', flat=True))
    return sorted(ids)


def offsets(task):
    """``{JEDI task id: input offset}`` of the recovery attempts."""
    out = {}
    for jedi, meta in task.panda_tasks.filter(jedi_task_id__isnull=False).values_list('jedi_task_id', 'metadata'):
        es = (meta or {}).get('event_service') or {}
        if 'input_offset' in es:
            out[int(jedi)] = int(es['input_offset'])
    return out


def credited(task):
    """``{file's last name component: [(first, last)]}``, PanDA's credited
    spans over every attempt, one-based file positions. JEDI removes a done
    task's event rows; such a task read each of its input files whole at its
    JEDI count, which it holds as finished, so those files are credited
    1..count. Any other attempt without event rows read nothing. A recovery
    attempt's JEDI events count from its input offset (the payload's skip is
    offset + startEvent - 1), so its spans are shifted by the offset."""
    out, shift = {}, offsets(task)
    with connections['panda'].cursor() as cursor:
        for jedi in attempts(task):
            base = shift.get(jedi, 0)
            cursor.execute('SELECT status FROM doma_panda.jedi_tasks WHERE jeditaskid = %s', [jedi])
            row = cursor.fetchone()
            if row is None:
                raise SystemExit(f'{task.name}: attempt {jedi} is not in PanDA')
            cursor.execute('SELECT count(*) FROM doma_panda.jedi_events WHERE jeditaskid = %s', [jedi])
            if cursor.fetchone()[0]:
                cursor.execute(
                    'SELECT d.lfn, e.def_min_eventid, e.def_max_eventid FROM doma_panda.jedi_events e '
                    'JOIN doma_panda.jedi_dataset_contents d ON d.jeditaskid = e.jeditaskid '
                    'AND d.datasetid = e.datasetid AND d.fileid = e.fileid '
                    'WHERE e.jeditaskid = %s AND e.status = ANY(%s)', [jedi, list(CREDITED)])
                for lfn, first, last in cursor.fetchall():
                    out.setdefault(lfn.removeprefix(evgen_es.LFN_PREFIX), []).append((base + int(first), base + int(last)))
            elif row[0] == 'done':
                cursor.execute(
                    "SELECT d.lfn, d.nevents, d.status FROM doma_panda.jedi_dataset_contents d "
                    "JOIN doma_panda.jedi_datasets s ON s.jeditaskid = d.jeditaskid AND s.datasetid = d.datasetid "
                    "WHERE d.jeditaskid = %s AND s.type = 'input'", [jedi])
                for lfn, nevents, status in cursor.fetchall():
                    if status != 'finished' or not nevents:
                        raise SystemExit(f'{task.name}: done attempt {jedi} holds {lfn} {status} '
                                         f'with {nevents} events; its credit cannot be read')
                    out.setdefault(lfn.removeprefix(evgen_es.LFN_PREFIX), []).append((base + 1, base + int(nevents)))
    return out


def carried(task):
    """``{(file, offset)}`` of the segments earlier recovery attempts carry."""
    out = set()
    for meta in task.panda_tasks.values_list('metadata', flat=True):
        es = (meta or {}).get('event_service') or {}
        if 'input_offset' in es:
            out.add((es['input_files'][0], es['input_offset']))
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('task', help="the task's composed name")
    parser.add_argument('--by')
    parser.add_argument('--reason')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--only', metavar='LFN:OFFSET')
    parser.add_argument('--limit', type=int)
    args = parser.parse_args()
    submit = bool(args.by) and not args.dry_run
    if submit and not args.reason:
        parser.error('--by needs --reason')
    task = ProdTask.objects.get(name=args.task)
    shapes = evgen_es.recovery(task, credited(task))
    done = carried(task)
    pending = [s for s in shapes if (s['input_files'][0], s['input_offset']) not in done]
    print(f'{args.task}: {len(shapes)} unread segments, {sum(s["events"] for s in shapes):,} events; '
          f'{len(shapes) - len(pending)} already carried')
    if args.only:
        lfn, _, offset = args.only.rpartition(':')
        pending = [s for s in pending if s['input_files'][0] == lfn and s['input_offset'] == int(offset)]
        if not pending:
            raise SystemExit(f'{args.task}: no pending segment {args.only}')
    if submit and pending:
        from pcs.commands import pinned_site
        if pinned_site(task) == 'NERSC_Perlmutter_epic_es':
            # Image admission reads the launcher's report, which refreshes only
            # while something reads it (NERSC_PERLMUTTER.md, Admission).
            from swf_epicprod.perlmutter_pilots import capability
            capability(refresh=True)
            if capability().get('mode') != 'bound':
                raise SystemExit('The Perlmutter launcher report is stale and this tree cannot store its '
                                 'refresh (a development tree never writes cached products): refresh it '
                                 'from the deployed monitor (the page\'s Update) and run again within 15 minutes')
    for shape in pending[:args.limit] if args.limit else pending:
        where = f'{shape["input_files"][0]} from {shape["input_offset"]:,}, {shape["events"]:,} events'
        if not submit:
            spec = build_evgen_task_params(task, panda_tasks=SimpleNamespace(metadata={'event_service': shape}))
            print(where, json.dumps({k: spec.get(k) for k in SPEC_KEYS}, default=str))
            continue
        attempt = services.prodtask_submit_tail(task=task, event_service=shape, changed_by=args.by,
                                                source='evgen_es_recovery')
        print(f'{where}: queued as {attempt.task_name}; reason: {args.reason}')


if __name__ == '__main__':
    main()
