#!/usr/bin/env python3
"""Stage a PCS task's EVGEN input for Event Service production on Perlmutter
(swf_epicprod/evgen_es.py; docs/NERSC_PERLMUTTER.md, EVGEN input in
production): copy the files its manifest covers from JLab to BNL_PROD_DISK_1,
checked against JLab's adler32, and register them with their event counts
in the task's input dataset. Idempotent; a file already registered is
checked, not copied again.

Run in the monitor Django environment with the testbed venv and ~/.env:

    evgen-es-stage.py group.EIC.26.07.3.epic_craterlake.p4497.e106.s11.r11
"""
import argparse
import json
import os

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django
django.setup()

from pcs.models import ProdTask
from swf_epicprod import evgen_es
from swf_epicprod.gke_pilots import settings as gke_settings


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('task', help="the task's composed name")
    parser.add_argument('--dry-run', action='store_true', help='print the coverage, copy nothing')
    args = parser.parse_args()
    task = ProdTask.objects.get(name=args.task)
    files, events = evgen_es.coverage(task)
    plan = {'dataset': evgen_es.input_dataset(task), 'events': events,
            'files': [{'lfn': evgen_es.lfn(p, e['ext']), 'used_events': e['events']}
                      for p, e in files.items()]}
    if args.dry_run:
        print(json.dumps(plan, indent=2))
        return
    print(json.dumps(evgen_es.stage(task, gke_settings()['proxy']), indent=2))


if __name__ == '__main__':
    main()
