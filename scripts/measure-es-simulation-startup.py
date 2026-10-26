#!/usr/bin/env python3
"""Measure process startup to the first Geant4 event in the campaign image."""
import argparse
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time


def inside(work, events):
    from DDSim.DD4hepSimulation import DD4hepSimulation
    from g4units import GeV, degree

    sim = DD4hepSimulation()
    sim.compactFile = ['/opt/detector/epic-26.07.3/share/epic/epic_craterlake_5x41.xml']
    sim.outputFile = str(work / 'simulation.edm4hep.root')
    sim.numberOfEvents = events
    sim.runType = 'run'
    sim.enableGun = True
    sim.printLevel = 3
    sim.random.seed = 528926730
    sim.random.enableEventSeed = True
    sim.filter.tracker = 'edep0'
    sim.gun.energy = 5 * GeV
    sim.gun.particle = 'neutron'
    sim.gun.position = (0., 0., 0.)
    sim.gun.direction = (0., 0., 1.)
    sim.gun.distribution = 'cos(theta)'
    sim.gun.thetaMin = 3 * degree
    sim.gun.thetaMax = 50 * degree
    sim.action.event = [{'name': 'Geant4TestEventAction/StartupTiming',
                         'parameter': {'OutputLevel': 3}}]
    return sim.run()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--events', type=int, default=1)
    parser.add_argument('--inside', action='store_true')
    parser.add_argument('--image', default='/cvmfs/singularity.opensciencegrid.org/eicweb/eic_xl:26.07.3-stable')
    args = parser.parse_args()
    if args.inside:
        return inside(args.work, args.events)
    args.work.mkdir(parents=True, exist_ok=False)
    root = Path(__file__).resolve().parents[1]
    core = min(os.sched_getaffinity(0))
    command = ['taskset', '-c', str(core), 'apptainer', 'exec', '--cleanenv',
               '--bind', f'{root}:/source:ro', '--bind', f'{args.work}:/evidence',
               '--pwd', '/evidence', args.image, 'stdbuf', '-oL', '-eL',
               'python3', '-u', '/source/scripts/' + Path(__file__).name,
               '--inside', '--work', '/evidence', '--events', str(args.events)]
    started = time.monotonic()
    first_event = None
    last_event_end = None
    with (args.work / 'timed.log').open('w') as output:
        process = subprocess.Popen(command, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True)
        for line in process.stdout:
            elapsed = time.monotonic() - started
            output.write(f'{elapsed:.6f} {line}')
            output.flush()
            if re.search(r'calling begin\(event_id=0\b', line):
                first_event = elapsed
                print(json.dumps({'first_event_start_s': elapsed}), flush=True)
            if re.search(r'calling end\(event_id=\d+\b', line):
                last_event_end = elapsed
        rc = process.wait()
    result = {'host': socket.gethostname(), 'image': args.image,
              'physical_cpu_affinity': core, 'events': args.events,
              'first_event_start_s': first_event, 'last_event_end_s': last_event_end,
              'total_s': time.monotonic() - started, 'returncode': rc,
              'includes_container_and_python_startup': True}
    (args.work / 'timing.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result), flush=True)
    if rc or first_event is None:
        print('Startup measurement failed; inspect timed.log', file=sys.stderr)
        return rc or 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
