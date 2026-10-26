#!/usr/bin/env python3
"""Prepare registered-output readback on the host; validate with campaign PODIO."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import sys


def prepare(args):
    sys.path.insert(0, '/opt/swf-monitor/current/src')
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')
    import django
    django.setup()
    from monitor_app.mcp.rucio import _load_catalog, _CATALOGS
    catalog = _load_catalog('es_output_readback', _CATALOGS['jlab_rucio']['env'])
    if args.prepare:
        evidence = json.loads(args.prepare.read_text())
        if not evidence.get('applied'):
            raise RuntimeError('The worker-loss evidence does not record an applied cut')
        files = evidence['registered_files']
        expected = set()
        for unit in evidence['record']['done']:
            span = unit['range']
            # PanDA spans are one-based and inclusive; EventHeader uses
            # the payload's zero-based skip offset (es_slot.run_unit).
            expected.update(range(int(span['startEvent']) - 1, int(span['lastEvent'])))
    else:
        scope, name = args.dataset.split(':', 1)
        result = catalog.list_files(name=name, scope=scope, page=1, limit=500)
        if result.get('error'):
            raise RuntimeError(result['error'])
        if (result.get('pagination') or {}).get('has_more'):
            raise RuntimeError('The output listing is incomplete')
        files = result.get('data', [])
        if args.record:
            record = json.loads(args.record.read_text())
            names = {c['did'].rsplit('/', 1)[-1] for c in record.get('closes', []) if c.get('ok')}
            files = [f for f in files if f['name'].rsplit('/', 1)[-1] in names]
            if {f['name'].rsplit('/', 1)[-1] for f in files} != names:
                raise RuntimeError('A recorded saved close is absent from the catalog')
            expected = set()
            for unit in record.get('done', []):
                span = unit['range']
                ids = set(range(int(span['startEvent']) - 1, int(span['lastEvent'])))
                if expected.intersection(ids):
                    raise RuntimeError('Saved unit event identities overlap')
                expected.update(ids)
            if not expected:
                raise RuntimeError('The record has no saved events to read')
        else:
            expected = set(range(args.events))
    if sum(int(f.get('events') or 0) for f in files) != len(expected):
        raise RuntimeError('Registered event counts do not equal the expected coverage')
    result = catalog.list_file_replicas(
        dids=[{'scope': f['scope'], 'name': f['name']} for f in files], page=1, limit=500)
    if result.get('error'):
        raise RuntimeError(result['error'])
    replicas = {f"{r['scope']}:{r['name']}": r for r in result.get('data', [])}
    inputs = []
    for f in files:
        r = replicas.get(f"{f['scope']}:{f['name']}")
        urls = [url for rse, paths in (r or {}).get('rses', {}).items()
                if r.get('states', {}).get(rse) in ('AVAILABLE', 'A')
                for url in paths if url.startswith('root://')]
        if not urls:
            raise RuntimeError('No available ROOT replica for ' + f['name'])
        inputs.append(dict(f, url=urls[0]))
    return {'files': inputs, 'expected_event_ids': sorted(expected),
            'expected_events': len(expected)}


def source_truth(path, numbers):
    """The final-state particles of the source EVGEN events with these event
    numbers, read by HepMC3 directly, in GeV: {event number: {'species':
    sorted PDG codes, 'exact': sorted (PDG, px, py, pz) of the particles not
    produced in a generator decay}}. npsim hands a generator decay (status 2)
    to Geant4 as a pre-assigned decay, so its products' lab momenta follow
    the parent through field and material; only their species is the
    source's."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'swf_epicprod' / 'payload' / 'es'))
    from evgen_ranges import hepmc, reader_for
    root, wanted, found = hepmc(), set(numbers), {}
    if not root.gInterpreter.Declare('#include <HepMC3/GenParticle.h>'):
        raise RuntimeError('Cannot load the HepMC3 particle interface')
    reader = reader_for(path)
    try:
        while wanted - set(found):
            event = root.HepMC3.GenEvent()
            if not reader.read_event(event) or reader.failed():
                break
            number = int(event.event_number())
            if number in wanted:
                scale = 0.001 if event.momentum_unit() == root.HepMC3.Units.MEV else 1.0
                final = [p for p in event.particles() if p.status() == 1]
                found[number] = {
                    'species': sorted(int(p.pid()) for p in final),
                    'exact': sorted((int(p.pid()), p.momentum().px() * scale, p.momentum().py() * scale,
                                     p.momentum().pz() * scale) for p in final
                                    if not any(q.status() == 2 for q in p.parents()))}
    finally:
        reader.close()
    missing = wanted - set(found)
    if missing:
        raise RuntimeError(f'{len(missing)} event numbers are absent from the source, e.g. {sorted(missing)[:5]}')
    return found


def same_particles(simulated, source, tolerance=1e-5):
    """Every final-state species of the source event, and every particle not
    produced in a generator decay at the source's momentum."""
    if sorted(p[0] for p in simulated) != source['species']:
        return False
    remaining = list(simulated)
    for expected in source['exact']:
        match = next((p for p in remaining if p[0] == expected[0] and
                      all(abs(x - y) <= tolerance * max(1.0, abs(y)) for x, y in zip(p[1:], expected[1:]))), None)
        if match is None:
            return False
        remaining.remove(match)
    return True


def validate(args):
    from podio import root_io
    evgen = getattr(args, 'evgen', False)
    manifest = json.loads(args.validate.read_text())
    source = getattr(args, 'source', None)
    expected_source = source_truth(source, manifest['expected_event_ids']) if source else None
    source_checked = 0
    ids, identities, truth = set(), set(), set()
    collections = None
    observed = []
    for entry in manifest['files']:
        reader = root_io.Reader(entry.get('local') or entry['url'])
        count = 0
        for frame in reader.get('events'):
            names = sorted(frame.getAvailableCollections())
            if collections is None:
                collections = names
            if names != collections:
                raise RuntimeError('The collection schema differs between events')
            if count == 0:
                for name in names:
                    len(frame.get(name))
            header = frame.get('EventHeader')[0]
            event = int(header.getEventNumber())
            identity = (int(header.getRunNumber()), event)
            if identity in identities or event in ids:
                raise RuntimeError('Duplicate event identity: ' + str(identity))
            identities.add(identity)
            ids.add(event)
            generated = [p for p in frame.get('MCParticles') if p.getGeneratorStatus() == 1]
            if evgen:
                if not generated:
                    raise RuntimeError('The reconstructed EVGEN event has no generated stable particles')
                values = [(p.getPDG(), p.getGeneratorStatus(),
                           p.getMomentum().x, p.getMomentum().y, p.getMomentum().z)
                          for p in generated]
                truth.add(hashlib.sha256(repr(values).encode()).hexdigest())
                if expected_source is not None:
                    simulated = sorted((p.getPDG(), p.getMomentum().x, p.getMomentum().y, p.getMomentum().z)
                                       for p in generated)
                    if event not in expected_source or not same_particles(simulated, expected_source[event]):
                        raise RuntimeError(f'Event {event}: generated particles differ from the source event')
                    source_checked += 1
                count += 1
                continue
            if len(generated) != 1 or generated[0].getPDG() != 2112:
                raise RuntimeError('The generated particle differs from the neutron-gun plan')
            q = generated[0].getMomentum()
            theta = math.degrees(math.atan2(math.hypot(q.x, q.y), q.z))
            if not 2.999 <= theta <= 50.001:
                raise RuntimeError('The generated particle is outside the angular range')
            key = hashlib.sha256(repr((q.x, q.y, q.z)).encode()).hexdigest()
            if key in truth:
                raise RuntimeError('Repeated generated truth')
            truth.add(key)
            count += 1
        if count != int(entry['events']):
            raise RuntimeError('The readable event count differs from the registered count')
        observed.append({'name': entry['name'], 'events': count})
    if ids != set(manifest['expected_event_ids']):
        diagnostic = args.output.with_name(args.output.stem + '-coverage-diagnostic.json')
        diagnostic.write_text(json.dumps({'observed_event_ids': sorted(ids),
            'expected_event_ids': manifest['expected_event_ids']}, indent=2) + '\n')
        raise RuntimeError('Readable event identities differ from the expected coverage')
    if args.fingerprints:
        args.fingerprints.write_text(json.dumps({
            'event_ids': sorted(ids), 'generated_truth_sha256': sorted(truth)}, indent=2) + '\n')
    result = {'events': len(ids), 'unique_event_ids': len(ids),
            'distinct_generated_truth': len(truth), 'collections': len(collections or []),
            'event_ids_sha256': hashlib.sha256(json.dumps(sorted(ids)).encode()).hexdigest(),
            'reader': 'podio.root_io.Reader', 'files': observed}
    if evgen:
        result['source_mode'] = 'evgen'
        if expected_source is not None:
            result.update(source=str(source), source_events_matched=source_checked)
    else:
        result.update(particle_pdg=2112, theta_degrees=[3, 50])
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument('--prepare', type=Path, help='applied cut evidence')
    mode.add_argument('--dataset', help='complete task output DID')
    mode.add_argument('--validate', type=Path, help='prepared readback manifest')
    ap.add_argument('--events', type=int, help='complete task event count')
    ap.add_argument('--evgen', action='store_true', help='external EVGEN truth instead of the neutron-gun plan')
    ap.add_argument('--source', help='with --evgen: the source EVGEN file; each event\'s final-state '
                                     'particles must match the source event of the same number')
    ap.add_argument('--record', type=Path, help='read only the saved closes and exact IDs in this shipped record')
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--fingerprints', type=Path,
                    help='retain checked identities for combining separately read closes')
    args = ap.parse_args()
    if args.dataset and not (args.events or args.record):
        ap.error('--dataset requires --events or --record')
    if args.record and not args.dataset:
        ap.error('--record requires --dataset')
    if args.fingerprints and not args.validate:
        ap.error('--fingerprints requires --validate')
    if args.source and not (args.validate and args.evgen):
        ap.error('--source requires --validate and --evgen')
    result = validate(args) if args.validate else prepare(args)
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({k: v for k, v in result.items() if k not in ('files', 'expected_event_ids')}))


if __name__ == '__main__':
    main()
