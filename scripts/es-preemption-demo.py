#!/usr/bin/env python3
"""Submit a fresh bounded gun sample; optionally execute an approved Google cut.

The existing generation plan, PCS composer and submit API supply the workload.
The cut is armed before dispatch and happens after thirty minutes, several
registered saves, and a recent completed save. A failed prerequisite removes
no worker. Evidence and production output remain after the demonstration.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from urllib.parse import quote, urlsplit
import uuid
import zlib


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, default=str) + '\n')


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--template', type=Path, required=True)
    ap.add_argument('--sample', required=True)
    ap.add_argument('--artifacts', type=Path, required=True)
    ap.add_argument('--approval-ref', required=True)
    ap.add_argument('--site', choices=('BNL_ePIC_GOOGLE_es', 'NERSC_Perlmutter_epic_es'),
                    default='BNL_ePIC_GOOGLE_es')
    ap.add_argument('--events', type=int, default=30000)
    ap.add_argument('--unit-events', type=int, default=250)
    ap.add_argument('--slots', default='auto')
    ap.add_argument('--memory-mb-per-core', type=int)
    ap.add_argument('--deadline-s', type=int, default=2400)
    ap.add_argument('--margin-s', type=int, default=300)
    ap.add_argument('--parallel-start', action='store_true')
    ap.add_argument('--priority', type=int)
    ap.add_argument('--observe-only', action='store_true',
                    help='submit and observe normal completion, without any worker removal')
    args = ap.parse_args()
    if args.site != 'BNL_ePIC_GOOGLE_es' and not args.observe_only:
        ap.error('controlled worker removal is implemented only for Google')
    own_token = os.environ['SWF_API_TOKEN']
    for raw in Path('/opt/swf-monitor/config/env/production.env').read_text().splitlines():
        raw = raw.strip().removeprefix('export ')
        if raw and not raw.startswith('#') and '=' in raw:
            k, v = raw.split('=', 1)
            os.environ[k.strip()] = v.strip().strip('"').strip("'")
    os.environ['SWF_API_TOKEN'] = own_token
    sys.path.insert(0, '/opt/swf-monitor/current/src')
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')
    import django
    django.setup()
    from django.conf import settings
    from django.db import connections
    from rest_framework.authtoken.models import Token
    from pcs.models import ProdTask
    from pcs.production_samples import compose
    from pcs.commands import build_evgen_task_params
    from swf_epicprod.production_es import gun_seed
    from swf_epicprod.gke_pilots import settings as gke_settings
    from rucio.client import Client
    from rucio.common.exception import DataIdentifierNotFound
    from XRootD import client as xrd
    import requests

    user = Token.objects.select_related('user').get(key=own_token).user
    if not user.is_active or user.username != 'wenaus':
        raise RuntimeError('The submission token must identify the approving operator')
    if args.site == 'BNL_ePIC_GOOGLE_es':
        cfg = gke_settings()
        if not cfg['enabled'] or int(cfg['max_pods']) != 5:
            raise RuntimeError('The existing five-worker Google commissioning configuration changed')
    from monitor_app import payload_reports
    payload_reports.SWEEPER_ENV = '/eic/u/wenauseic/.epic-report-sweeper.env'
    store = payload_reports.sweeper_client()
    if store is None:
        raise RuntimeError('The record reader is unavailable; no task will be submitted')
    args.artifacts.mkdir(parents=True, exist_ok=True)
    plan = json.loads((args.template / 'gun-generation-plan.json').read_text())
    es = json.loads((args.template / 'event-service.json').read_text())
    sample, events = args.sample, args.events
    stem = 'neutron_5GeV_3to50deg.' + sample
    previous = set()
    for old, n in [('gES1', 10000), ('pES1', 1000), ('gES2', 100000),
                   ('pES2', 1000000), ('gES3', 30000)]:
        if old == sample:
            continue
        previous.update(gun_seed('neutron_5GeV_3to50deg.' + old, i) for i in range(n))
    if any(gun_seed(stem, i) in previous for i in range(events)):
        raise RuntimeError('The proposed sample overlaps an existing random stream')
    plan.update(events=events, sample=sample, seed_stem=stem)
    dataset = 'group.EIC.esplan.26.07.3.p1107.' + sample
    es.update(events=events, input_events=events, events_per_job=events,
              close_s=300, deadline_s=args.deadline_s, margin_s=args.margin_s,
              unit_events=args.unit_events, parallel_start=args.parallel_start,
              slots=args.slots,
              input_dataset='group.EIC:' + dataset,
              row=f'SINGLE/neutron/5GeV/3to50deg.{sample}/{stem},steer,{events},0000')
    if args.memory_mb_per_core is not None:
        es['memory_mb_per_core'] = args.memory_mb_per_core
    write_json(args.artifacts / 'gun-generation-plan.json', plan)
    write_json(args.artifacts / 'event-service.json', es)
    data = (args.artifacts / 'gun-generation-plan.json').read_bytes()
    lfn = 'group.EIC.gunplan.26.07.3.p1107.' + sample + '.json'
    adler = f'{zlib.adler32(data) & 0xffffffff:08x}'
    with tempfile.TemporaryDirectory(prefix='es-plan-', dir='/data/swf-tmp') as temp:
        proxy = Path(temp) / 'proxy'
        proxy.write_bytes(subprocess.check_output(
            ['sudo', '-n', 'cat', settings.RUCIO_BNL_X509_PROXY]))
        proxy.chmod(0o600)
        os.environ.update(X509_USER_PROXY=str(proxy), X509_CERT_DIR='/etc/grid-security/certificates',
                          RUCIO_CONFIG='/cvmfs/eic.opensciencegrid.org/rucio-clients/rucio.cfg')
        catalog = Client(rucio_host=settings.RUCIO_BNL_URL, auth_host=settings.RUCIO_BNL_URL,
                         account=settings.RUCIO_BNL_ACCOUNT, vo=settings.RUCIO_BNL_VO,
                         auth_type='x509_proxy', creds={'client_proxy': str(proxy)}, ca_cert=None)
        try:
            meta = catalog.get_metadata('group.EIC', lfn)
            if meta['bytes'] != len(data) or meta['adler32'] != adler:
                raise RuntimeError('The existing generation plan differs')
        except DataIdentifierNotFound:
            pfn = catalog.lfns2pfns('BNL_PROD_DISK_1', ['group.EIC:' + lfn],
                                    operation='write', scheme='root')['group.EIC:' + lfn]
            parsed = urlsplit(pfn)
            fs = xrd.FileSystem(f'{parsed.scheme}://{parsed.netloc}')
            status, info = fs.stat(parsed.path)
            if status.ok:
                check, checksum = fs.query(xrd.flags.QueryCode.CHECKSUM, parsed.path)
                if info.size != len(data) or not check.ok or adler.encode() not in checksum:
                    raise RuntimeError('The staged generation plan differs')
            elif status.errno == 3011:
                subprocess.run(['xrdcp', '--cksum', 'adler32:' + adler,
                                str(args.artifacts / 'gun-generation-plan.json'), pfn], check=True)
            else:
                raise RuntimeError(str(status))
            catalog.add_dataset('group.EIC', dataset)
            catalog.add_replicas('BNL_PROD_DISK_1', [{'scope': 'group.EIC', 'name': lfn,
                'bytes': len(data), 'adler32': adler, 'meta': {'guid': uuid.uuid4().hex, 'events': events}}])
            catalog.attach_dids('group.EIC', dataset, [{'scope': 'group.EIC', 'name': lfn}])
            catalog.add_replication_rule([{'scope': 'group.EIC', 'name': dataset}], 1, 'BNL_PROD_DISK_1')
            catalog.close('group.EIC', dataset)
    write_json(args.artifacts / 'bnl-input.json', {'dataset': es['input_dataset'], 'file': lfn,
               'events': events, 'sha256': hashlib.sha256(data).hexdigest()})
    name = 'group.EIC.26.07.3.epic_craterlake.p1107.e37.s11.r11.3to50deg.' + sample
    task = ProdTask.objects.filter(name=name).first()
    if task is None:
        task = compose(ProdTask.objects.get(pk=8524), sample=sample, event_service=es,
                       site=args.site, created_by=user.username)
    if args.priority is not None and not task.panda_task_id and not task.panda_tasks.exists():
        overrides = dict(task.overrides or {}, task_priority=args.priority)
        url = ('https://pandaserver02.sdcc.bnl.gov/swf-monitor/pcs/api/prod-tasks/'
               + quote(task.composed_name, safe='') + '/')
        response = requests.patch(url, headers={'Authorization': 'Token ' + own_token},
                                  json={'overrides': overrides}, timeout=40,
                                  allow_redirects=False)
        response.raise_for_status()
        if response.is_redirect:
            raise RuntimeError('Task priority update redirected')
        task.refresh_from_db()
    spec = build_evgen_task_params(task)
    if spec['nJobs'] != 1 or spec['event_service']['close_s'] != 300:
        raise RuntimeError('The composed task does not match the approved shape')
    write_json(args.artifacts / 'submission-spec.json', spec)
    evidence = {'operator': user.username, 'approval_ref': args.approval_ref,
                'task': task.composed_name, 'sample': sample, 'events': events,
                'site': args.site, 'close_s': 300,
                'deadline_s': args.deadline_s, 'margin_s': args.margin_s,
                'parallel_start': args.parallel_start,
                'slots': args.slots,
                'cut_after_s': None if args.observe_only else 1800,
                'observe_only': args.observe_only,
                'armed_at': datetime.now(timezone.utc).isoformat()}
    write_json(args.artifacts / 'armed.json', evidence)
    print(json.dumps(evidence), flush=True)
    if not task.panda_task_id and not task.panda_tasks.exists():
        url = ('https://pandaserver02.sdcc.bnl.gov/swf-monitor/pcs/api/prod-tasks/'
               + quote(task.composed_name, safe='') + '/submit/')
        response = requests.post(url, headers={'Authorization': 'Token ' + own_token},
                                 json={'now': True, 'site': args.site}, timeout=40,
                                 allow_redirects=False)
        response.raise_for_status()
        if response.is_redirect or 'application/json' not in response.headers.get('Content-Type', ''):
            raise RuntimeError(f'Submission returned HTTP {response.status_code} '
                               f"with {response.headers.get('Content-Type')}")
        write_json(args.artifacts / 'submission-response.json', response.json())
    watch_started = time.monotonic()
    module_spec = importlib.util.spec_from_file_location(
        'es_preempt_worker', Path(__file__).with_name('es-preempt-google-worker.py'))
    preempt = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(preempt)
    reader, bucket, prefix = store
    shown = None
    while time.monotonic() - watch_started < 3000:
        task.refresh_from_db()
        taskid = task.panda_task_id
        if not taskid:
            time.sleep(10)
            continue
        evidence['jedi_task_id'] = taskid
        write_json(args.artifacts / 'armed.json', evidence)
        if args.observe_only:
            print(json.dumps({'task': taskid, 'observe_only': True}), flush=True)
            return 0
        with connections['panda'].cursor() as cursor:
            cursor.execute('SELECT pandaid FROM doma_panda.jobsactive4 '
                           'WHERE jeditaskid=%s AND jobstatus=%s ORDER BY pandaid', [taskid, 'running'])
            jobs = [int(row[0]) for row in cursor.fetchall()]
        if shown != jobs:
            print(json.dumps({'task': taskid, 'running_jobs': jobs}), flush=True)
            shown = jobs
        for pid in jobs:
            try:
                record = json.loads(reader.get_object(
                    Bucket=bucket, Key=f'{prefix}{pid}/es.json')['Body'].read())
            except reader.exceptions.NoSuchKey:
                continue
            if record.get('ended_at') or time.time() - float(record['started_at']) < 1800:
                continue
            closes = [c for c in record.get('closes', []) if c.get('ok')]
            if not closes or time.time() - float(closes[-1]['ended_at']) > 120:
                continue
            return preempt.main(['--pandaid', str(pid), '--task', str(taskid),
                '--dataset', 'epic:' + spec['outputs'][0]['dataset'], '--by', user.username,
                '--approval-ref', args.approval_ref, '--evidence',
                str(args.artifacts / 'cut.json'), '--apply'])
        time.sleep(10)
    raise RuntimeError('The bounded demonstration window expired without a qualifying cut')


if __name__ == '__main__':
    raise SystemExit(main())
