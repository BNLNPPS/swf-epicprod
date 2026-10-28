#!/usr/bin/env python3
"""Stage a PCS task's EVGEN input at NERSC, ahead of its Event Service jobs
on Perlmutter (swf_epicprod/nersc_stage.py; docs/NERSC_PERLMUTTER.md, EVGEN
input in production).

    evgen-es-nersc-stage.py proxy                 our proxy to NERSC, private
    evgen-es-nersc-stage.py probe                 what a transfer node offers
    evgen-es-nersc-stage.py stage TASK [--wait]   copy the task's missing files from
                                                  where its catalog says they reside
    evgen-es-nersc-stage.py stage-dataset DID [--wait]  a registered input dataset (a canary's)
    evgen-es-nersc-stage.py job JOBID             a transfer job's state

Run in the monitor Django environment with the testbed venv and ~/.env.
"""
import argparse
import json
import os
import time
from pathlib import Path

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django
django.setup()

from swf_epicprod import evgen_es, nersc_stage
from swf_epicprod.gke_pilots import settings as gke_settings
from swf_epicprod.nersc_iri import Client


def stage_dataset(client, proxy, did, wait, names=()):
    """A dataset's files copied to NERSC from where its catalog says they
    reside: an ``epic:/EVGEN/...`` dataset from JLab's catalog (``names``, if
    given, limits it to those file names), any other from PanDA's (a BNL
    registration, as the earlier canaries' inputs)."""
    import tempfile
    from swf_epicprod.evgen_es import STAGE_DIR, _catalogs, _jlab
    scope, name = did.split(':', 1)
    nersc_stage.ensure_proxy(client, proxy)
    if name.startswith('/'):
        jlab = _jlab()
        files = [{'name': f['name'], 'lfn': os.path.basename(f['name']), 'scope': f['scope'],
                  'bytes': f['bytes'], 'adler32': f['adler32']}
                 for f in jlab.list_files(scope, name)
                 if not names or f['name'] in names or os.path.basename(f['name']) in names]
        todo = nersc_stage.missing(client, files)
        sources = nersc_stage.catalog_urls(todo) if todo else {}
    else:
        with tempfile.TemporaryDirectory(prefix='nersc-stage-proxy-', dir=STAGE_DIR) as temp:
            private = Path(temp) / 'proxy'
            private.write_bytes(Path(proxy).read_bytes())
            private.chmod(0o600)
            bnl, _ = _catalogs(str(private))
            files = [{'lfn': f['name'], 'bytes': f['bytes'], 'adler32': f['adler32']}
                     for f in bnl.list_files(scope, name)]
        todo = nersc_stage.missing(client, files)
        sources = nersc_stage.bnl_urls(proxy, [f['lfn'] for f in todo]) if todo else {}
    print(f'{did}: {len(files)} files, {len(todo)} to copy')
    if not todo:
        return
    job_name = 'stage-dataset-' + time.strftime('%Y%m%dT%H%M%S')
    job_id = client.submit(nersc_stage.job_spec(job_name, nersc_stage.copy_script(todo, sources, nersc_stage.COPIER)))
    print('submitted', job_id, job_name)
    if wait:
        nersc_stage.wait_job(client, job_id, timeout_s=nersc_stage.DURATION_S)
        print(client.view(f'{nersc_stage.JOB_DIR}/{job_name}.out'))
        left = nersc_stage.missing(client, files)
        print(f'{len(left)} files still missing at NERSC')
        raise SystemExit(1 if left else 0)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('action', choices=('proxy', 'probe', 'mdprobe', 'ioprobe', 'quota', 'stage',
                                           'stage-dataset', 'job'))
    parser.add_argument('target', nargs='?')
    parser.add_argument('--wait', action='store_true')
    parser.add_argument('--files', help='stage-dataset: only these file names (comma-separated)')
    args = parser.parse_args()
    client = Client()
    proxy = gke_settings()['proxy']
    if args.action == 'proxy':
        print(json.dumps({k: v for k, v in nersc_stage.ensure_proxy(client, proxy).items()
                          if k in ('size', 'mode', 'uid', 'mtime')}))
    elif args.action in ('probe', 'mdprobe', 'ioprobe', 'quota'):
        name = args.action + '-' + time.strftime('%Y%m%dT%H%M%S')
        script = {'probe': nersc_stage.PROBE, 'mdprobe': nersc_stage.METADATA_PROBE,
                  'ioprobe': nersc_stage.IO_PROBE, 'quota': nersc_stage.QUOTA_PROBE}[args.action]
        job_id = client.submit(nersc_stage.job_spec(name, script, duration_s=600))
        print('submitted', job_id, name)
        state = nersc_stage.wait_job(client, job_id, timeout_s=1200)
        print(json.dumps(state)[:800])
        print(client.view(f'{nersc_stage.JOB_DIR}/{name}.out'))
    elif args.action == 'stage-dataset':
        stage_dataset(client, proxy, args.target, args.wait,
                      names=[n for n in (args.files or '').split(',') if n])
    elif args.action == 'job':
        print(json.dumps(client.job(args.target), indent=1)[:2000])
    else:
        from pcs.models import ProdTask
        sample = next(p for p in args.target.split('.') if p[:1] == 'p' and p[1:].isdigit())
        task = next(t for t in ProdTask.objects.filter(name__contains=sample)
                    if t.composed_name == args.target)
        staged = evgen_es.catalog_input(task)
        done, detail = nersc_stage.ensure_staged(task, staged, proxy, client)
        print(f'{args.target}: {detail}')
        record = (task.overrides or {}).get('site_input') or {}
        if not done and args.wait and record.get('job_id'):
            nersc_stage.wait_job(client, record['job_id'], timeout_s=nersc_stage.DURATION_S)
            print(client.view(f'{nersc_stage.JOB_DIR}/{record["name"]}.out'))
            left = nersc_stage.missing(client, staged['files'])
            print(f'{len(left)} files still missing at NERSC')
            raise SystemExit(1 if left else 0)

if __name__ == '__main__':
    main()
