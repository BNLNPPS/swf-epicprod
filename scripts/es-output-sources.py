#!/usr/bin/env python3
"""List each registered Event Service output of a task with the EVGEN file it came from.

An ES job reads one EVGEN file, and each of its closes carries the job's PanDA
id in its file name (<stem>.<pandaid>.<close>.eicrecon.edm4eic.root, es-dedup.py
job_of). The source of an output is therefore the one input file of its job's
credited ranges (JEDI_Events finished or done, 3 or 6). This holds whatever the
<stem> says: tasks 41525 and 41526 ran without EPICPROD_SWITCH_ES_INPUT_NAMES,
so every close is named run001 (NERSC_PERLMUTTER.md, Production).

  es-output-sources.py --task N --dataset SCOPE:DID --output SOURCES.csv     deployed venv (catalog, PanDA)

The run stops, writing nothing, if the dataset listing fails, if a job's
credited ranges span more than one file, or if an output's job holds no
credit. It prints a summary: outputs, registered events, per source file the
outputs and events, and per job registered against credited events.
"""
import argparse
import collections
import csv
import importlib.util
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location('es_dedup', HERE / 'es-dedup.py')
es_dedup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(es_dedup)


def listing(catalog, scope, name):
    files, page = [], 1
    while True:
        result = catalog.list_files(name=name, scope=scope, page=page, limit=1000)
        if result.get('error'):
            raise RuntimeError(result['error'])
        files.extend(result.get('data', []))
        if not (result.get('pagination') or {}).get('has_more'):
            return files
        page += 1


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--task', type=int, required=True, help='JEDI task id')
    ap.add_argument('--dataset', required=True, help='the task output dataset, SCOPE:NAME')
    ap.add_argument('--output', type=Path, required=True, help='CSV: output, events, pandaid, source EVGEN file')
    args = ap.parse_args()

    es_dedup.django_setup()
    from django.db import connections
    from monitor_app.mcp.rucio import _load_catalog, _CATALOGS
    catalog = _load_catalog('es_output_sources', _CATALOGS['jlab_rucio']['env'])
    scope, name = args.dataset.split(':', 1)
    files = listing(catalog, scope, name)
    if not files:
        raise RuntimeError(f'{args.dataset} lists no files')

    with connections['panda'].cursor() as cursor:
        cursor.execute(
            'SELECT e.pandaid, d.lfn, SUM(e.def_max_eventid - e.def_min_eventid + 1) '
            'FROM doma_panda.jedi_events e JOIN doma_panda.jedi_dataset_contents d '
            'ON d.jeditaskid = e.jeditaskid AND d.datasetid = e.datasetid AND d.fileid = e.fileid '
            'WHERE e.jeditaskid = %s AND e.status IN (3, 6) GROUP BY e.pandaid, d.lfn', [args.task])
        source, credited = {}, {}
        for pandaid, lfn, events in cursor.fetchall():
            pandaid = int(pandaid)
            if pandaid in source and source[pandaid] != lfn:
                raise RuntimeError(f'job {pandaid} holds credit in two files: {source[pandaid]}, {lfn}')
            source[pandaid], credited[pandaid] = lfn, int(events)

    rows, registered = [], collections.Counter()
    for f in sorted(files, key=lambda f: f['name']):
        pandaid = es_dedup.job_of(f['name'])
        if pandaid not in source:
            raise RuntimeError(f"{f['name']}: job {pandaid} holds no credit in task {args.task}")
        events = int(f.get('events') or 0)
        registered[pandaid] += events
        rows.append((f['name'].rsplit('/', 1)[-1], events, pandaid, source[pandaid]))

    with args.output.open('w', newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow(['output', 'events', 'pandaid', 'source_evgen_file'])
        writer.writerows(rows)

    by_file = collections.defaultdict(lambda: {'outputs': 0, 'events': 0, 'jobs': set()})
    for _, events, pandaid, lfn in rows:
        by_file[lfn]['outputs'] += 1
        by_file[lfn]['events'] += events
        by_file[lfn]['jobs'].add(pandaid)
    summary = {
        'task': args.task, 'dataset': args.dataset, 'outputs': len(rows),
        'registered_events': sum(registered.values()), 'credited_events': sum(credited.values()),
        'by_source_file': {k: {'outputs': v['outputs'], 'events': v['events'], 'jobs': len(v['jobs'])}
                           for k, v in sorted(by_file.items())},
        'jobs_registered_ne_credited': {str(p): [registered[p], credited[p]]
                                        for p in sorted(credited) if registered[p] != credited[p]},
    }
    print(json.dumps(summary, indent=1))


if __name__ == '__main__':
    sys.exit(main())
