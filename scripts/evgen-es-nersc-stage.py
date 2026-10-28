#!/usr/bin/env python3
"""Stage a PCS task's EVGEN input at NERSC, ahead of its Event Service jobs
on Perlmutter (swf_epicprod/nersc_stage.py; docs/NERSC_PERLMUTTER.md, EVGEN
input in production).

    evgen-es-nersc-stage.py proxy                 our proxy to NERSC, private
    evgen-es-nersc-stage.py probe                 what a transfer node offers
    evgen-es-nersc-stage.py stage TASK [--wait]   copy the task's missing files
    evgen-es-nersc-stage.py stage-ready           every ready EVGEN task the front
                                                  can feed a whole-node queue; by cron
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


STATE = '/data/swf-tmp/nersc-stage-state.json'
RUNNING = ('pending', 'queued', 'running', 'configuring', 'new', 'active', 'submitted')


def ready_tasks():
    """The composed names of the ready tasks the front marks EVGEN-ES capable
    (its stored state, every pool and queue)."""
    from monitor_app.models import CachedProduct
    p = CachedProduct.objects.filter(key='front_state').first()
    state = p.value if p and not isinstance(p.value, str) else json.loads(p.value) if p else {}
    names = []
    for entries in (state.get('backlog') or {}).values():
        for e in entries or []:
            if e.get('evgen_es') and e.get('task') not in names:
                names.append(e['task'])
    return names


def stage_task(client, proxy, name, inflight):
    from pcs.models import ProdTask
    sample = next(p for p in name.split('.') if p[:1] == 'p' and p[1:].isdigit())
    task = next(t for t in ProdTask.objects.filter(name__contains=sample) if t.composed_name == name)
    staged = evgen_es.stage(task, proxy)
    todo = nersc_stage.missing(client, staged['files'])
    if not todo:
        return f'{name}: all {len(staged["files"])} files at NERSC'
    sources = nersc_stage.bnl_urls(proxy, [f['lfn'] for f in todo])
    job_name = f'stage-{sample}-' + time.strftime('%Y%m%dT%H%M%S')
    job_id = client.submit(nersc_stage.job_spec(
        job_name, nersc_stage.copy_script(todo, sources, nersc_stage.COPIER)))
    inflight[name] = {'job_id': job_id, 'name': job_name, 'at': time.time(), 'files': len(todo)}
    return f'{name}: {len(todo)} of {len(staged["files"])} files to copy, job {job_id} ({job_name})'


def stage_ready(client, proxy):
    """Stage every ready EVGEN-ES task's input at NERSC ahead of its feed,
    one copy job a task at a time: a task whose copy is still running is
    left to it (the state file names the job)."""
    try:
        inflight = json.loads(Path(STATE).read_text())
    except (OSError, ValueError):
        inflight = {}
    nersc_stage.ensure_proxy(client, proxy)
    for name in ready_tasks():
        job = inflight.get(name)
        if job:
            try:
                st = client.job(job['job_id'])
                status = st.get('status') or {}
                status = str(status.get('state') if isinstance(status, dict) else status).lower()
            except Exception as e:                            # noqa: BLE001
                status = f'unknown ({e})'
            if status in RUNNING or status.startswith('unknown'):
                print(f'{name}: copy job {job["job_id"]} {status}')
                continue
            inflight.pop(name)
        try:
            print(stage_task(client, proxy, name, inflight))
        except Exception as e:                                # noqa: BLE001
            print(f'ERROR {name}: {type(e).__name__}: {e}')
    Path(STATE).write_text(json.dumps(inflight, indent=1) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('action', choices=('proxy', 'probe', 'mdprobe', 'ioprobe', 'quota', 'stage',
                                           'stage-ready', 'job'))
    parser.add_argument('target', nargs='?')
    parser.add_argument('--wait', action='store_true')
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
    elif args.action == 'stage-ready':
        stage_ready(client, proxy)
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
