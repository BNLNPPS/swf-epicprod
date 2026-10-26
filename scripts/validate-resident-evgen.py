#!/usr/bin/env python3
"""Check range preparation, persistent EVGEN execution, replay and cold parity."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_events(path):
    from podio import root_io
    reader = root_io.Reader(str(path))
    result = []
    for frame in reader.get('events'):
        names = sorted(frame.getAvailableCollections())
        sizes = {name: len(frame.get(name)) for name in names}
        header = frame.get('EventHeader')[0]
        truth = []
        for particle in frame.get('MCParticles'):
            if not particle.getGeneratorStatus():
                continue
            momentum = particle.getMomentum()
            vertex = particle.getVertex()
            truth.append([particle.getPDG(), particle.getGeneratorStatus(),
                          momentum.x, momentum.y, momentum.z,
                          vertex.x, vertex.y, vertex.z])
        result.append({'event_number': int(header.getEventNumber()),
                       'truth': truth, 'collections': sizes})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--geometry', default='/opt/detector/epic-26.07.3/share/epic/epic_craterlake_5x130.xml')
    args = parser.parse_args()
    work = args.work.resolve()
    work.mkdir(parents=True, exist_ok=False)
    root = Path(__file__).resolve().parents[1]
    kit = root / 'swf_epicprod/payload/es'
    ranges = load(kit / 'evgen_ranges.py', 'evgen_ranges')
    resident = load(kit / 'resident_sim.py', 'resident_sim')
    prepared = [ranges.prepare(args.input, first, 2, work / f'input-{i}.hepmc3')
                for i, first in enumerate((13, 2, 13))]
    if prepared[0]['selected'] != prepared[2]['selected']:
        raise RuntimeError('Preparing the same source range changed event content')
    state = work / 'resident'
    sock = f'/tmp/resident-evgen-validation-{os.getpid()}.sock'
    try:
        replies, output_events = [], []
        for index, manifest in enumerate(prepared):
            output = work / f'output-{index}.root'
            reply_path = work / f'request-{index}.json'
            command = [sys.executable, str(kit / 'resident_sim.py'), 'request',
                '--socket', sock, '--state-dir', str(state), '--reply', str(reply_path), '--',
                '--runType', 'batch', '--compactFile', args.geometry, '--filter.tracker', 'edep0',
                '--printLevel', 'WARNING', '--random.seed', str(314159 if index != 1 else 271828),
                '--random.enableEventSeed', '--numberOfEvents', '2', '--outputFile', str(output),
                '--hepmc3.useHepMC3', 'true', '--inputFiles', str(Path(args.input).resolve()),
                '--skipNEvents', str(manifest['first_event'])]
            subprocess.run(command, cwd=work, stdout=subprocess.DEVNULL, check=True, timeout=900)
            reply = json.loads(reply_path.read_text())
            replies.append(reply)
            actual = json.loads((state / f'range-{index + 1}.hepmc3.json').read_text())
            if actual['selected'] != manifest['selected']:
                raise RuntimeError('Resident preprocessing changed source event content')
            events = read_events(output)
            expected = [e['event_number'] for e in manifest['selected']]
            if [e['event_number'] for e in events] != expected:
                raise RuntimeError(f'Input identities changed: expected {expected}, got {events}')
            output_events.append(events)
            print(json.dumps({'request': index, 'pid': reply['pid'],
                'events': expected, 'request_wall_s': reply['ended_at'] - reply['started_at']}), flush=True)
        if len({r['pid'] for r in replies}) != 1 or [r['sequence'] for r in replies] != [1, 2, 3]:
            raise RuntimeError('EVGEN requests did not reuse one initialized simulation')
        if output_events[0] != output_events[2]:
            raise RuntimeError('EVGEN replay changed generated truth or collection sizes')
        cold = work / 'cold.root'
        with (work / 'cold.log').open('w') as cold_log:
            subprocess.run(['npsim', '--runType', 'batch', '--inputFiles', prepared[0]['file'],
                '--hepmc3.useHepMC3', 'true', '--compactFile', args.geometry,
                '--filter.tracker', 'edep0', '--printLevel', 'WARNING',
                '--random.seed', '314159', '--numberOfEvents', '2', '--outputFile', str(cold)],
                cwd=work, stdout=cold_log, stderr=subprocess.STDOUT, check=True, timeout=600)
        if read_events(cold) != output_events[0]:
            raise RuntimeError('Persistent EVGEN output differs from normal cold npsim')
        result = {'status': 'passed', 'resident_pid': replies[0]['pid'], 'requests': replies,
                  'prepared': prepared, 'events': output_events, 'cold_reference_matches': True,
                  'replay_matches': True, 'input_event_content_verified': True}
        resident.write_json(work / 'validation.json', result)
        print(json.dumps({k: v for k, v in result.items() if k not in ('events', 'requests', 'prepared')}), flush=True)
    finally:
        if Path(sock).exists():
            resident.exchange(sock, {'command': 'stop'}, 10)


if __name__ == '__main__':
    main()
