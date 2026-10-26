#!/usr/bin/env python3
"""Validate successive seeded gun requests in the matching campaign image."""
import argparse
import importlib.util
import json
import math
import os
from pathlib import Path
import subprocess
import sys


def read_events(path):
    from podio import root_io
    reader = root_io.Reader(str(path))
    result = []
    for frame in reader.get('events'):
        names = sorted(frame.getAvailableCollections())
        sizes = {name: len(frame.get(name)) for name in names}
        header = frame.get('EventHeader')[0]
        primaries = [p for p in frame.get('MCParticles') if p.getGeneratorStatus() == 1]
        if len(primaries) != 1 or primaries[0].getPDG() != 2112:
            raise RuntimeError('Expected one generated neutron')
        p = primaries[0].getMomentum()
        theta = math.degrees(math.atan2(math.hypot(p.x, p.y), p.z))
        if not 3 <= theta <= 50:
            raise RuntimeError('Generated neutron outside the configured angles')
        result.append({'id': int(header.getEventNumber()),
                       'truth': [p.x, p.y, p.z], 'collections': sizes})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, required=True)
    args = parser.parse_args()
    work = args.work.resolve()
    work.mkdir(parents=True, exist_ok=False)
    root = Path(__file__).resolve().parents[1]
    driver = root / 'swf_epicprod/payload/es/resident_sim.py'
    spec = importlib.util.spec_from_file_location('resident_sim', driver)
    resident = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(resident)
    steering = work / 'gun.steer'
    steering.write_text('from DDSim.DD4hepSimulation import DD4hepSimulation\n'
        'from g4units import GeV, degree\nSIM = DD4hepSimulation()\n'
        'SIM.gun.energy = 5*GeV\nSIM.gun.particle = "neutron"\n'
        'SIM.gun.position = (0., 0., 0.)\nSIM.gun.direction = (0., 0., 1.)\n'
        'SIM.gun.distribution = "cos(theta)"\n'
        'SIM.gun.thetaMin = 3*degree\nSIM.gun.thetaMax = 50*degree\n')
    fixed = ['--runType', 'run', '--enableGun', '--steeringFile', str(steering),
             '--compactFile', '/opt/detector/epic-26.07.3/share/epic/epic_craterlake_5x41.xml',
             '--filter.tracker', 'edep0', '--printLevel', 'WARNING']
    socket_path = f'/tmp/resident-validation-{os.getpid()}.sock'
    replies, events = [], []
    try:
        for index, (seed, offset) in enumerate(((528926730, 0), (528926732, 2), (528926730, 0))):
            output, reply = work / f'request-{index}.root', work / f'request-{index}.json'
            command = [sys.executable, str(driver), 'request', '--socket', socket_path,
                '--state-dir', str(work / 'resident'), '--reply', str(reply), '--']
            command += fixed + ['--random.enableEventSeed', '--random.seed', str(seed),
                '--numberOfEvents', '2', '--meta.eventNumberOffset', str(offset),
                '--outputFile', str(output)]
            subprocess.run(command, cwd=work, check=True)
            replies.append(json.loads(reply.read_text()))
            events.append(read_events(output))
        if len({r['pid'] for r in replies}) != 1 or [r['sequence'] for r in replies] != [1, 2, 3]:
            raise RuntimeError('Requests did not reuse one initialized simulation')
        if [[e['id'] for e in group] for group in events] != [[0, 1], [2, 3], [0, 1]]:
            raise RuntimeError('Incorrect request event identities')
        if events[0] != events[2]:
            raise RuntimeError('Replaying a seed and interval changed truth or collection sizes')
        if {tuple(e['truth']) for e in events[0]} & {tuple(e['truth']) for e in events[1]}:
            raise RuntimeError('Different requests repeated generated truth')
        # A cold npsim with the same stream must agree with the resident
        # request, including the complete collection schema and sizes.
        cold = work / 'cold.root'
        with (work / 'cold.log').open('w') as log:
            subprocess.run(['npsim'] + fixed + ['--random.seed', '528926730',
                '--numberOfEvents', '2', '--outputFile', str(cold)],
                cwd=work, stdout=log, stderr=subprocess.STDOUT, check=True)
        if read_events(cold) != events[0]:
            raise RuntimeError('Resident output differs from the equivalent cold npsim request')
        result = {'status': 'passed', 'requests': replies, 'events': events,
                  'cold_reference_matches': True, 'replay_matches': True,
                  'resident_pid': replies[0]['pid']}
        (work / 'validation.json').write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps({k: v for k, v in result.items() if k not in ('events', 'requests')}), flush=True)
    finally:
        if Path(socket_path).exists():
            resident.exchange(socket_path, {'command': 'stop'}, 10)


if __name__ == '__main__':
    main()
