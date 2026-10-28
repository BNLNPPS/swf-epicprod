#!/usr/bin/env python3
"""Stage a PCS task's EVGEN input at NERSC, ahead of its Event Service jobs
on Perlmutter (swf_epicprod/nersc_stage.py; docs/NERSC_PERLMUTTER.md, EVGEN
input in production).

    evgen-es-nersc-stage.py proxy                 our proxy to NERSC, private
    evgen-es-nersc-stage.py probe                 what a transfer node offers
    evgen-es-nersc-stage.py stage TASK [--wait]   copy the task's missing files
    evgen-es-nersc-stage.py job JOBID             a transfer job's state

Run in the monitor Django environment with the testbed venv and ~/.env.
"""
import argparse
import json
import os
import time

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django
django.setup()

from swf_epicprod import evgen_es, nersc_stage
from swf_epicprod.gke_pilots import settings as gke_settings
from swf_epicprod.nersc_iri import Client


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('action', choices=('proxy', 'probe', 'stage', 'job'))
    parser.add_argument('target', nargs='?')
    parser.add_argument('--wait', action='store_true')
    args = parser.parse_args()
    client = Client()
    proxy = gke_settings()['proxy']
    if args.action == 'proxy':
        print(json.dumps({k: v for k, v in nersc_stage.ensure_proxy(client, proxy).items()
                          if k in ('size', 'mode', 'uid', 'mtime')}))
    elif args.action == 'probe':
        name = 'probe-' + time.strftime('%Y%m%dT%H%M%S')
        job_id = client.submit(nersc_stage.job_spec(name, nersc_stage.PROBE, duration_s=600))
        print('submitted', job_id, name)
        state = nersc_stage.wait_job(client, job_id, timeout_s=1200)
        print(json.dumps(state)[:800])
        print(client.view(f'{nersc_stage.JOB_DIR}/{name}.out'))
    elif args.action == 'job':
        print(json.dumps(client.job(args.target), indent=1)[:2000])
    else:
        from pcs.models import ProdTask
        sample = next(p for p in args.target.split('.') if p[:1] == 'p' and p[1:].isdigit())
        task = next(t for t in ProdTask.objects.filter(name__contains=sample)
                    if t.composed_name == args.target)
        nersc_stage.ensure_proxy(client, proxy)
        staged = evgen_es.stage(task, proxy)
        todo = nersc_stage.missing(client, staged['files'])
        print(f'{args.target}: {len(staged["files"])} files, {len(todo)} to copy to {nersc_stage.CFS_DIR}')
        if not todo:
            return
        sources = nersc_stage.bnl_urls(proxy, [f['lfn'] for f in todo])
        name = f'stage-{sample}-' + time.strftime('%Y%m%dT%H%M%S')
        job_id = client.submit(nersc_stage.job_spec(
            name, nersc_stage.copy_script(todo, sources, nersc_stage.COPIER)))
        print('submitted', job_id, name)
        if args.wait:
            print(json.dumps(nersc_stage.wait_job(client, job_id, timeout_s=nersc_stage.DURATION_S))[:800])
            print(client.view(f'{nersc_stage.JOB_DIR}/{name}.out'))
            left = nersc_stage.missing(client, staged['files'])
            print(f'{len(left)} files still missing at NERSC')
            raise SystemExit(1 if left else 0)


if __name__ == '__main__':
    main()
