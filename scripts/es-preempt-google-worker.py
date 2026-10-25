#!/usr/bin/env python3
"""Remove one approved ES worker after verifying its registered output.

The operation removes the Kubernetes pod, with zero termination grace, while
leaving PanDA's Event Service close-out and range recovery to their normal path.
Run with the production runtime and an explicit operator decision reference.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--pandaid', type=int, required=True)
    ap.add_argument('--task', type=int, required=True)
    ap.add_argument('--dataset', required=True)
    ap.add_argument('--by', required=True)
    ap.add_argument('--approval-ref', required=True)
    ap.add_argument('--evidence', type=Path, required=True)
    ap.add_argument('--apply', action='store_true')
    args = ap.parse_args(argv)
    for raw in Path('/opt/swf-monitor/config/env/production.env').read_text().splitlines():
        raw = raw.strip().removeprefix('export ')
        if raw and not raw.startswith('#') and '=' in raw:
            key, value = raw.split('=', 1)
            os.environ[key.strip()] = value.strip().strip('"').strip("'")
    sys.path.insert(0, '/opt/swf-monitor/current/src')
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')
    import django
    django.setup()
    from django.db import connections
    from monitor_app import payload_reports
    from monitor_app.mcp.rucio import _load_catalog, _CATALOGS
    from swf_epicprod.gke_pilots import settings, _clients, LABEL
    from kubernetes.client import V1DeleteOptions, V1Preconditions

    def active_job():
        with connections['panda'].cursor() as cursor:
            cursor.execute('SELECT "modificationhost" FROM doma_panda.jobsactive4 '
                           'WHERE "pandaid"=%s AND "jeditaskid"=%s AND "jobstatus"=%s '
                           'AND "computingsite"=%s',
                           [args.pandaid, args.task, 'running', 'BNL_ePIC_GOOGLE_es'])
            row = cursor.fetchone()
        if not row or not row[0]:
            raise RuntimeError('The selected Google job is no longer running')
        return row[0]

    pod_name = active_job()
    payload_reports.SWEEPER_ENV = '/eic/u/wenauseic/.epic-report-sweeper.env'
    store = payload_reports.sweeper_client()
    if store is None:
        raise RuntimeError('The report reader credential is unavailable')
    client, bucket, prefix = store
    record = json.loads(client.get_object(
        Bucket=bucket, Key=f'{prefix}{args.pandaid}/es.json')['Body'].read())
    if str(record.get('pandaid')) != str(args.pandaid) or record.get('ended_at'):
        raise RuntimeError('The current harness record is not an active job record')
    if time.time() - float(record['written_at']) > 90:
        raise RuntimeError('The harness record is stale')
    elapsed = time.time() - float(record['started_at'])
    closes = [c for c in record.get('closes', []) if c.get('ok')]
    saved = sum(int(c.get('events') or 0) for c in closes)
    if elapsed < 1800 or len(closes) < 3 or saved < 1000 or not record.get('in_flight'):
        raise RuntimeError('The worker does not meet the approved demonstration conditions')

    scope, dataset = args.dataset.split(':', 1)
    closes = [dict(c, did=c['did'] if ':' in c['did'] else scope + ':' + c['did'])
              for c in closes]
    catalog = _load_catalog('preemption_readback', _CATALOGS['jlab_rucio']['env'])
    result = catalog.list_files(name=dataset, scope=scope, page=1, limit=500)
    if result.get('error'):
        raise RuntimeError(result['error'])
    files = {f"{f['scope']}:{f['name']}": f for f in result.get('data', [])}
    for close in closes:
        entry = files.get(close.get('did'))
        if entry is None or int(entry.get('events') or 0) != int(close['events']):
            raise RuntimeError(f"Registered close is not verified: {close.get('did')}")

    cfg = settings()
    core, _ = _clients(cfg)
    pod = core.read_namespaced_pod(pod_name, cfg['namespace'])
    if pod.status.phase != 'Running' or pod.metadata.labels.get('app') != LABEL:
        raise RuntimeError('The selected pod is not a running production ES worker')
    if active_job() != pod_name:
        raise RuntimeError('The job changed worker during preparation')
    evidence = {'pandaid': args.pandaid, 'task': args.task, 'operator': args.by,
                'approval_ref': args.approval_ref, 'pod': pod_name,
                'pod_uid': pod.metadata.uid, 'node': pod.spec.node_name,
                'at': datetime.now(timezone.utc).isoformat(), 'elapsed_s': elapsed,
                'saved_events': saved, 'registered_closes': len(closes),
                'registered_files': [files[c['did']] for c in closes],
                'record': record, 'applied': False}
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    if args.evidence.exists():
        raise RuntimeError('The evidence destination already exists')
    args.evidence.write_text(json.dumps(evidence, indent=2, default=str) + '\n')
    if args.apply:
        core.delete_namespaced_pod(pod_name, cfg['namespace'], grace_period_seconds=0,
                                  body=V1DeleteOptions(grace_period_seconds=0,
                                      preconditions=V1Preconditions(uid=pod.metadata.uid)))
        evidence['applied'] = True
        evidence['cut_at'] = datetime.now(timezone.utc).isoformat()
        args.evidence.write_text(json.dumps(evidence, indent=2, default=str) + '\n')
    print(json.dumps({k: evidence[k] for k in ('pandaid', 'pod', 'elapsed_s',
                     'saved_events', 'registered_closes', 'applied')}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
