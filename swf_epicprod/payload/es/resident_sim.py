#!/usr/bin/env python3
"""Keep the image's npsim setup alive for gun or prepared EVGEN requests.

The image's npsim entry point performs its normal EIC configuration. Its
DDG4 kernel run call is replaced by the request loop, after initialization
and before its normal termination. Each request owns one random stream,
one zero-based event interval and one completed EDM4hep file.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import resource
import runpy
import shlex
import shutil
import socket
import subprocess
import sys
import time


def exchange(path, payload, timeout=1800):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(timeout)
        connection.connect(path)
        with connection.makefile('rwb') as stream:
            stream.write(json.dumps(payload).encode() + b'\n')
            stream.flush()
            line = stream.readline(1 << 20)
    if not line:
        raise RuntimeError('Resident simulation exited without a reply')
    reply = json.loads(line)
    if reply.get('status') not in ('completed', 'stopped'):
        raise RuntimeError(reply.get('error') or 'Resident simulation failed')
    return reply


def write_json(path, value):
    temporary = Path(str(path) + '.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.replace(path)


def configuration(argv):
    """Separate range identity from the fixed detector and source setup."""
    values, fixed = {'steering': ''}, []
    varying = {'--random.seed': 'seed', '--numberOfEvents': 'events',
               '--meta.eventNumberOffset': 'event_offset', '--outputFile': 'output_file',
               '--inputFiles': 'input_file', '--skipNEvents': 'first_event'}
    index = 0
    while index < len(argv):
        flag = argv[index]
        if flag in varying:
            if index + 1 == len(argv):
                raise ValueError('Missing value for ' + flag)
            values[varying[flag]] = argv[index + 1]
            index += 2
        elif flag == '--random.enableEventSeed':
            # A resident request seeds the complete stream explicitly. The
            # stock event seeder caches its first seed and includes run ID.
            index += 1
        elif flag == '--steeringFile':
            path = Path(argv[index + 1]).resolve()
            values['steering'] = path.read_text()
            index += 2
        else:
            fixed.append(flag)
            index += 1
    mode = 'evgen' if values.get('input_file') else 'gun'
    if ('--enableGun' in fixed) != (mode == 'gun'):
        raise ValueError('Resident simulation needs exactly one input source')
    if mode == 'gun' and not values['steering']:
        raise ValueError('Resident gun needs a steering file')
    for name in ('seed', 'events', 'event_offset', 'first_event'):
        values[name] = int(values.get(name, 0))
    if values['seed'] <= 0 or values['events'] <= 0 or min(values['event_offset'], values['first_event']) < 0:
        raise ValueError('Invalid seed, event count or event offset')
    if mode == 'evgen':
        if values['event_offset']:
            raise ValueError('EVGEN retains source event numbers; an additional offset is invalid')
        if '://' not in values['input_file']:
            values['input_file'] = str(Path(values['input_file']).resolve())
        values['source_id'] = os.environ.get('EPICPROD_EVGEN_SOURCE_ID') or values['input_file']
    values['source_mode'] = mode
    values['output_file'] = str(Path(values['output_file']).resolve())
    signature = hashlib.sha256(json.dumps([mode, fixed, values['steering']]).encode()).hexdigest()
    values['configuration_sha256'] = signature
    return fixed, values


def request(args):
    fixed, payload = configuration(args.npsim_args)
    state = Path(args.state_dir).resolve()
    state.mkdir(parents=True, exist_ok=True)
    descriptor = state / 'resident.json'
    started = time.time()
    child = None
    if not descriptor.exists():
        steering = payload['steering']
        if payload['source_mode'] == 'evgen':
            # DDSim supplies SIM as the image's configured runner. Replacing
            # it would silently lose npsim's EIC particle-truth settings.
            steering += '\nimport sys\nsys.path.insert(0, ' + repr(str(Path(__file__).resolve().parent)) + ')\n'
            steering += 'from evgen_source import configure\nconfigure(SIM, ' + repr(str(state / 'current-range')) + ')\n'
        (state / 'source.steer').write_text(steering)
        setup = fixed + ['--steeringFile', str(state / 'source.steer'),
                         '--random.seed', str(payload['seed']), '--numberOfEvents', '1',
                         '--outputFile', str(state / 'unused.edm4hep.root')]
        write_json(descriptor, {'status': 'starting',
                   'initialization_started_at': time.time(),
                   'source_mode': payload['source_mode'],
                   'configuration_sha256': payload['configuration_sha256']})
        with (state / 'resident.log').open('ab') as log:
            child = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), 'serve',
                 '--socket', args.socket, '--state-dir', str(state), '--'] + setup,
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                # Keep the slot's process group, so a slot cut also ends
                # its resident simulation. A normal unit exit leaves it up.
                cwd=os.getcwd())
        deadline = time.monotonic() + args.startup_timeout
        while not (Path(args.socket).exists() and
                   json.loads(descriptor.read_text()).get('status') == 'ready'):
            if child.poll() is not None:
                raise RuntimeError(f'Resident npsim exited {child.returncode}; see {state / "resident.log"}')
            if time.monotonic() >= deadline:
                child.terminate()
                raise TimeoutError(f'Resident npsim initialization exceeded {args.startup_timeout}s')
            time.sleep(0.2)
    info = json.loads(descriptor.read_text())
    if info.get('configuration_sha256') != payload['configuration_sha256']:
        raise RuntimeError('The resident detector or gun configuration differs from this request')
    if info.get('status') != 'ready':
        raise RuntimeError(f'Resident npsim is {info.get("status")}; see {state / "resident.log"}')
    payload.pop('steering')
    reply = exchange(args.socket, payload, args.request_timeout)
    reply['request_started_at'] = started
    reply['startup_wait_s'] = round(reply['started_at'] - started, 6)
    reply['resident_log'] = str(state / 'resident.log')
    write_json(args.reply, reply)
    print(json.dumps(reply), flush=True)


def serve(args):
    import DDG4
    import ROOT
    DDG4.import_geant4_class('G4RunManager', 'G4RunManager.hh')
    if not ROOT.gInterpreter.Declare('#include "' + str(Path(__file__).with_name('resident_timing.h')) + '"'):
        raise RuntimeError('Cannot load the resident event-timing action')
    state = Path(args.state_dir)
    descriptor = state / 'resident.json'
    info = json.loads(descriptor.read_text())
    launched_at = info['initialization_started_at']

    def resident_run(handle):
        kernel = handle.get()
        output = kernel.eventAction().get('EDM4hepOutput')
        if not output:
            raise RuntimeError('The configured npsim EDM4hep output action is missing')
        handle.UI = ''
        random = DDG4.Sim.Geant4Random.instance()
        timing = ROOT.epicprod.installTiming(kernel)
        ROOT.SetOwnership(timing, False)
        header = dict(DDG4._evalProperty(DDG4.Interface.getProperty(output, 'RunHeader').data))

        def set_property(name, value):
            if not DDG4.Interface.setProperty(output, name, str(value)):
                raise RuntimeError('Cannot set EDM4hep output property ' + name)

        sequence = 0
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            # Publish readiness only after initialization and listen().
            listener.bind(args.socket)
            listener.listen(1)
            info.update(status='ready', pid=os.getpid(), initialized_at=time.time(),
                        initialization_started_at=launched_at,
                        seed_mode='request-stream', requests=0)
            write_json(descriptor, info)
            while True:
                connection, _ = listener.accept()
                with connection, connection.makefile('rwb') as stream:
                    payload = json.loads(stream.readline(1 << 20))
                    if payload.get('command') == 'stop':
                        stream.write(b'{"status":"stopped"}\n')
                        stream.flush()
                        return 1
                    started_at = time.time()
                    cpu_started = time.process_time()
                    try:
                        if payload['configuration_sha256'] != info['configuration_sha256']:
                            raise ValueError('Resident configuration mismatch')
                        count, seed, offset = (int(payload[k]) for k in ('events', 'seed', 'event_offset'))
                        if count <= 0 or seed <= 0 or offset < 0:
                            raise ValueError('Invalid request values')
                        path = str(Path(payload['output_file']).resolve())
                        if Path(path).exists():
                            raise FileExistsError(path)
                        input_range = None
                        if info.get('source_mode') == 'evgen':
                            from evgen_ranges import prepare
                            range_file = state / f'range-{sequence + 1}.hepmc3'
                            manifest = prepare(payload['input_file'], int(payload['first_event']), count,
                                               range_file, payload.get('source_id'))
                            control = state / 'current-range'
                            temporary = state / 'current-range.tmp'
                            temporary.write_text(str(range_file) + '\n')
                            temporary.replace(control)
                            input_range = {k: manifest[k] for k in
                                ('schema', 'source_id', 'first_event', 'events', 'sha256', 'started_at', 'ended_at')}
                            input_range['event_content_sha256'] = hashlib.sha256(
                                json.dumps(manifest['selected'], sort_keys=True).encode()).hexdigest()
                        set_property('Output', path)
                        set_property('EventNumberOffset', offset)
                        request_header = dict(header, **{'random.seed': str(seed),
                            'random.enableEventSeed': 'False', 'numberOfEvents': str(count),
                            'meta.eventNumberOffset': str(offset), 'outputFile': path,
                            'epicprod.seedMode': 'request-stream'})
                        if input_range:
                            request_header.update({'inputFiles': payload['input_file'],
                                'skipNEvents': str(payload['first_event']),
                                'epicprod.inputRangeSHA256': input_range['sha256']})
                        request_header['CommandLine'] = shlex.join(
                            [str(entry)] + args.npsim_args + ['--random.seed', str(seed),
                             '--numberOfEvents', str(count), '--meta.eventNumberOffset', str(offset),
                             '--outputFile', path])
                        set_property('RunHeader', request_header)
                        random.setSeed(seed)
                        marker = state / 'first-event.txt'
                        marker.write_text('0\n')
                        timing.reset(str(marker))
                        progress = dict(payload, pid=os.getpid(), sequence=sequence + 1,
                                        initialization_started_at=launched_at,
                                        initialized_at=info['initialized_at'], started_at=started_at)
                        write_json(state / 'request.json', progress)
                        # Request identity and random stream must not depend on
                        # the number of requests previously handled by this slot.
                        kernel.runManager().SetRunIDCounter(0)
                        if not kernel.runEvents(count):
                            raise RuntimeError('DDG4 runEvents failed')
                        sequence += 1
                        reply = dict(status='completed', pid=os.getpid(), sequence=sequence,
                                     seed=seed, seed_mode='request-stream', events=count,
                                     event_offset=offset, output_file=path,
                                     configuration_sha256=info['configuration_sha256'],
                                     initialization_started_at=launched_at,
                                     initialized_at=info['initialized_at'],
                                     started_at=started_at, ended_at=time.time(),
                                     first_event_at=float(timing.first) or None,
                                     last_event_at=float(timing.last) or None,
                                     cpu_s=time.process_time() - cpu_started,
                                     resident_peak_rss_kb=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
                        if input_range:
                            reply['input_range'] = input_range
                        info['requests'] = sequence
                        write_json(descriptor, info)
                        write_json(state / 'request.json', reply)
                    except Exception as error:
                        info.update(status='failed', error=str(error))
                        write_json(descriptor, info)
                        reply = dict(status='failed', error=str(error), pid=os.getpid())
                    stream.write(json.dumps(reply).encode() + b'\n')
                    stream.flush()
                    if reply['status'] != 'completed':
                        raise RuntimeError(reply['error'])

    # KernelHandle is already Pythonized by DDG4. The image's entry point
    # still supplies all detector, optical-physics and particle actions.
    DDG4.Kernel.run = resident_run
    executable = Path(os.path.realpath(shutil.which('npsim') or ''))
    entry = executable.with_name('npsim.py')
    if not entry.is_file():
        raise RuntimeError(f'Cannot locate the image npsim entry point beside {executable}')
    sys.argv = [str(entry)] + args.npsim_args
    try:
        runpy.run_path(str(entry), run_name='__main__')
    finally:
        if Path(args.socket).exists():
            Path(args.socket).unlink()
        info['status'] = 'stopped' if info.get('status') == 'ready' else info.get('status', 'failed')
        write_json(descriptor, info)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('request', 'serve', 'stop'))
    parser.add_argument('--socket', required=True)
    parser.add_argument('--state-dir')
    parser.add_argument('--reply', type=Path)
    parser.add_argument('--startup-timeout', type=int, default=600)
    parser.add_argument('--request-timeout', type=int, default=1800)
    args, remaining = parser.parse_known_args()
    args.npsim_args = remaining[1:] if remaining[:1] == ['--'] else remaining
    if args.command == 'stop':
        print(json.dumps(exchange(args.socket, {'command': 'stop'}, 10)))
    elif args.command == 'request':
        request(args)
    else:
        serve(args)


if __name__ == '__main__':
    main()
