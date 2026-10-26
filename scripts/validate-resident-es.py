#!/usr/bin/env python3
"""Run the existing ES harness and registered-output reader on npps0."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from types import SimpleNamespace


def image_command(root, work, command):
    return ['apptainer', 'exec', '--cleanenv', '--bind', str(root) + ':/source:ro',
            '--bind', str(work) + ':/evidence', '--bind', '/home/wenaus',
            '--pwd', '/evidence',
            '/cvmfs/singularity.opensciencegrid.org/eicweb/eic_xl:26.07.3-stable',
            '/bin/bash', '-c', 'source /opt/detector/epic-26.07.3/bin/thisepic.sh && '
            + shlex.join(command)]


def catalog():
    from rucio.client import Client
    os.environ['X509_USER_PROXY'] = '/home/wenaus/creds/eicprod-proxy-for-jlab'
    os.environ['RUCIO_CONFIG'] = '/source/swf_epicprod/payload/rucio.cfg'
    return Client(rucio_host='https://rucio-server.jlab.org:443',
                  auth_host='https://rucio-server.jlab.org:443', account='eicprod',
                  auth_type='x509_proxy',
                  creds={'client_proxy': os.environ['X509_USER_PROXY']})


def inside(args):
    work = Path('/evidence')
    dataset = '/TEST/canary/' + args.stamp
    client = catalog()
    if args.phase == 'create':
        client.add_dataset('epic', dataset)
        client.add_replication_rule([{'scope': 'epic', 'name': dataset}], 1,
                                    'EIC-XRD', lifetime=604800)
        print(json.dumps({'dataset': 'epic:' + dataset, 'created': True}), flush=True)
        return
    files = list(client.list_files('epic', dataset))
    replicas = list(client.list_replicas([{'scope': f['scope'], 'name': f['name']} for f in files]))
    by_name = {f['name']: f for f in replicas}
    for entry in files:
        candidates = [url for url in by_name[entry['name']]['pfns'] if url.startswith('root://')]
        if not candidates:
            raise RuntimeError('No ROOT replica for ' + entry['name'])
        entry['url'] = candidates[0]
    manifest = work / 'readback-manifest.json'
    manifest.write_text(json.dumps({'files': files, 'expected_event_ids': list(range(20)),
                                    'expected_events': 20}, default=str))
    spec = importlib.util.spec_from_file_location('es_readback', '/source/scripts/es-readback.py')
    reader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reader)
    result = reader.validate(SimpleNamespace(validate=manifest, output=work / 'readback.json',
                                             fingerprints=None))
    (work / 'readback.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path)
    parser.add_argument('--template', type=Path)
    parser.add_argument('--stamp', required=True)
    parser.add_argument('--phase', choices=('create', 'read'))
    args = parser.parse_args()
    if args.phase:
        return inside(args)
    root = Path(__file__).resolve().parents[1]
    work = args.work.resolve()
    work.mkdir(parents=True, exist_ok=False)
    sandbox = work / 'sandbox'
    sandbox.mkdir()
    (sandbox / 'payload').symlink_to(root / 'swf_epicprod/payload', target_is_directory=True)
    env = dict(json.loads(args.template.read_text())['env'])
    env.update(OUT_RSE='EIC-XRD', COPYFULL='false', COPYRECO='true', COPYLOG='false',
               COPYEVGEN='false', USERUCIO='true', TAG_PREFIX=args.stamp,
               X509_USER_PROXY='/home/wenaus/creds/eicprod-proxy-for-jlab',
               EPICPROD_SWITCH_RESIDENT_SIM='1', EPICPROD_SWITCH_ES_GUN_EVENTS='1')
    (sandbox / 'environment-loopback.sh').write_text(''.join(
        'export ' + key + '=' + shlex.quote(str(value)) + '\n' for key, value in env.items()))
    stem = 'neutron_5GeV_3to50deg.' + args.stamp
    path = 'SINGLE/neutron/5GeV/3to50deg.' + args.stamp + '/' + stem
    (sandbox / 'loopback.csv').write_text(path + ',steer,20,0000\n')
    ranges = [{'eventRangeID': 'resident-' + str(i), 'startEvent': i, 'lastEvent': i,
               'LFN': 'internal-gun-plan.json', 'GUID': 'resident-gun-validation'} for i in range(1, 21)]
    (work / 'ranges.json').write_text(json.dumps(ranges))
    helper = ['python3', '/source/scripts/validate-resident-es.py', '--stamp', args.stamp]
    subprocess.run(image_command(root, work, helper + ['--phase', 'create']), check=True)
    command = ['python3', str(root / 'swf_epicprod/payload/es/es_harness.py'),
        '--slots', '1', '--work', str(work / 'harness'), '--sandbox', str(sandbox),
        '--payload', str(root / 'swf_epicprod/payload'), '--csv-base', 'loopback',
        '--image', '/cvmfs/singularity.opensciencegrid.org/eicweb/eic_xl:26.07.3-stable',
        '--stamp', args.stamp, '--ranges-file', str(work / 'ranges.json'),
        '--events-per-unit', '5', '--close-s', '30', '--deadline-s', '900', '--margin-s', '180',
        '--summary', str(work / 'es-summary.json'),
        '--env', 'CANARY_OUTPUT_DATASET=TEST/canary/' + args.stamp,
        '--env', 'CANARY_LIFETIME_S=604800']
    with (work / 'harness.log').open('w') as log:
        subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)
    summary = json.loads((work / 'es-summary.json').read_text())
    units = summary['done']
    if summary['failed'] or len(units) != 4 or sum(u['events'] for u in units) != 20:
        raise RuntimeError('The harness did not complete four five-event units')
    pids = {u['simulation']['pid'] for u in units}
    if len(pids) != 1 or sorted(u['simulation']['sequence'] for u in units) != [1, 2, 3, 4]:
        raise RuntimeError('The harness did not reuse its resident simulation')
    for unit in units:
        sim = unit['simulation']
        if not sim['started_at'] <= sim['first_event_at'] <= sim['last_event_at'] <= sim['ended_at']:
            raise RuntimeError('Simulation event boundaries are not ordered')
        if not unit.get('reconstruction', {}).get('started_at') or not unit.get('phases'):
            raise RuntimeError('The unit did not retain its measured processing phases')
    if not summary['closes'] or not all(c.get('ok') for c in summary['closes']):
        raise RuntimeError('A rolling close failed')
    subprocess.run(image_command(root, work, helper + ['--phase', 'read']), check=True)
    print(json.dumps({'status': 'passed', 'events': 20, 'units': len(units),
                      'resident_pid': next(iter(pids)), 'dataset': 'epic:/TEST/canary/' + args.stamp}), flush=True)


if __name__ == '__main__':
    main()
