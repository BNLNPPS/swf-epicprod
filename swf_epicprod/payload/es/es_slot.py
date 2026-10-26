#!/usr/bin/env python3
"""One slot of the node harness (docs/NODE_EVENT_DISPATCHER.md): runs
inside the task's image for the life of the job and processes the range
units the front end stages into its inbox, one at a time, through the
production payload as a chunk of the manifest row (run.sh), with the
input the harness staged once and the reconstruction by the slot's
resident EICrecon (run.sh's EPICPROD_INPUT_LOCAL and EPICPROD_RECO_SOCKET).

The inbox/outbox contract is WORK_UNIT_CONTRACT.md's: a unit is
``inbox/<unit_id>.unit.json``, its result ``outbox/<unit_id>/`` with
``unit.json`` and the ``done`` marker last, or ``error.json`` in place of
``done``. A unit spec here carries the pilot's range (eventRangeID,
startEvent, lastEvent, LFN) and the manifest row's path and extension;
the slot maps the range to run.sh's chunk arithmetic (start is
one-based; a chunk is the range's length of events, skipped start-1).

    es_slot.py --work <slot dir> --payload <payload dir> --input <staged file>
               [--env KEY=VALUE ...] [--idle-exit <seconds>]

Exits 0 when the front end writes ``inbox/STOP``, or after --idle-exit
seconds with an empty inbox (0 = never). The resident EICrecon is ended
on exit.
"""
import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time

import resident_unit


def log(msg):
    print(f"[es_slot {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def sock_path(args):
    """The slot's resident EICrecon socket: a Unix socket path is at most
    108 bytes, so it lives in /tmp under a short name, not in the work
    tree (a scratchpad path broke ZeroMQ's bind)."""
    return f'/tmp/esreco-{os.getpid()}.sock'


def unit_tmpdir(args):
    """Scratch for the slot's units: EPICPROD_TMPDIR when set, else tmp/ in
    the slot's work tree, which the container binds."""
    d = os.environ.get('EPICPROD_TMPDIR') or os.path.join(args.work, 'tmp')
    os.makedirs(d, exist_ok=True)
    return d


def run_dir(args):
    """The slot's working directory for run.sh: the sandbox's top-level
    files linked in, the slot's own caches and outputs beside them."""
    d = os.path.join(args.work, 'run')
    os.makedirs(d, exist_ok=True)
    for name in os.listdir(args.sandbox):
        src, dst = os.path.join(args.sandbox, name), os.path.join(d, name)
        if os.path.isfile(src) and not os.path.lexists(dst):
            os.symlink(src, dst)
    return d


def next_unit(inbox):
    names = sorted(n for n in os.listdir(inbox) if n.endswith('.unit.json'))
    return os.path.join(inbox, names[0]) if names else None


def unit_timing(out, work, since, report=None):
    """The same measured phases for completed units and live snapshots."""
    errors = []

    def read(path):
        try:
            with open(path) as stream:
                return json.load(stream)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as error:
            errors.append(f'{path}: {error}')
            return {}

    result = {}
    if report is None:
        report = read(os.path.join(out, 'payload-report.json'))
    phases = {name: {key: stage.get(key) for key in ('started_at', 'ended_at')}
              for name, stage in (report.get('stages') or {}).items()
              if name in ('simulation', 'reconstruction') and stage.get('started_at')}
    if phases:
        result['phases'] = phases
    simulation = read(os.path.join(out, 'simulation.json'))
    if not simulation:
        simulation = read(os.path.join(work, 'simulation', 'request.json'))
        if float(simulation.get('started_at') or 0) < since:
            simulation = {}
        if simulation:
            try:
                with open(os.path.join(work, 'simulation', 'first-event.txt')) as stream:
                    first = float(stream.read())
                if first > 0:
                    simulation['first_event_at'] = first
            except FileNotFoundError:
                pass
            except (OSError, ValueError) as error:
                errors.append(str(error))
    if not simulation:
        startup = read(os.path.join(work, 'simulation', 'resident.json'))
        if float(startup.get('initialization_started_at') or 0) >= since:
            simulation = startup
    if simulation:
        result['simulation'] = simulation
    reconstruction = read(os.path.join(out, 'reconstruction.json'))
    if reconstruction:
        startup = read(os.path.join(work, 'reconstruction-startup.json'))
        result['reconstruction'] = dict(startup,
            started_at=reconstruction.get('request_started_at'),
            ended_at=reconstruction.get('request_ended_at'))
    if errors:
        result['timing_error'] = '; '.join(errors)
    return result


def run_unit(spec_path, args):
    with open(spec_path) as f:
        spec = json.load(f)
    uid = spec['unit_id']
    rng = spec['range']
    start, last = int(rng['startEvent']), int(rng['lastEvent'])
    count = last - start + 1
    out = os.path.join(args.work, 'outbox', uid)
    os.makedirs(out, exist_ok=True)
    record = {'contract_version': 1, 'unit_id': uid, 'range': rng,
              'events': count, 'started_at': time.time()}
    if count < 1:
        record.update(status='error', message=f'range {start}-{last} is empty')
        return uid, record, None
    label = None
    if spec.get('block') is not None:
        # A unit of a fine-grained task: the block of the front end's K
        # events names the chunk; the unit may be a part of the block
        # (the file's last events, or events left after a partial job),
        # so the payload is told where to start explicitly, and a unit
        # that does not open its block names its outputs by its start
        # too, apart from the block's own.
        block, per_unit = int(spec['block']), int(spec['unit_events'])
        chunk = f"{block:04d}"
        if start - 1 != block * per_unit:
            label = f"{chunk}s{start}"
    elif (start - 1) % count:
        record.update(status='error', message=f'range {start}-{last} is not a whole chunk of its own length')
        return uid, record, None
    else:
        chunk = f"{(start - 1) // count:04d}"
    # run.sh runs in the slot's own run directory (a link to each of the
    # sandbox's files: it sources environment-*.sh and the proxy from
    # the working directory, and takes its output root from it too), not
    # in the shared sandbox: the geometry's file loader makes its
    # calibrations/ cache under the working directory on the first
    # npsim, and concurrent slots in one directory race on it (three of
    # four cold ranges died at "parent path calibrations/onnx cannot be
    # created"). The outputs of concurrent ranges are told apart by the
    # chunk in their names; each range's report and logs go to its outbox.
    env = dict(os.environ)
    for kv in args.env or []:
        k, _, v = kv.partition('=')
        env[k] = v
    handoff = os.path.join(args.work, 'handoff', uid) if args.handoff else ''
    env.update({
        'EPICPROD_REPORT_ID': f"{env.get('PANDAID') or 'unidentified'}/{uid}",
        'EPICPROD_RECO_HANDOFF': handoff,
        'EPICPROD_SKIP_EVENTS': str(start - 1),
        'EPICPROD_CHUNK_LABEL': label or '',
        'EPICPROD_RECO_SOCKET': sock_path(args),
        'EPICPROD_SIM_SOCKET': f'/tmp/essim-{os.getpid()}.sock',
        'EPICPROD_SIM_STATE': os.path.join(args.work, 'simulation'),
        'EPICPROD_SIM_REPLY': os.path.join(out, 'simulation.json'),
        'EPICPROD_RECO_STATE': os.path.join(args.work, 'reconstruction-startup.json'),
        'EPICPROD_RECO_REPLY': os.path.join(out, 'reconstruction.json'),
        'REGISTRATION_STAGGER_MAX_S': '0',
        'EPICPROD_CALL_HOME': '0',          # the harness calls home for the job
        'EPICPROD_NTHREADS': '1',           # the slots are the job's parallelism
        # Scratch in the job's own work tree (run.sh makes TMPDIR/<pid> and
        # removes it at exit), never the host's /tmp: the container binds
        # the host /tmp, and on npps0 that is the small root volume, which
        # six units filled on 2026-09-24 (job 3618756).
        'TMPDIR': unit_tmpdir(args),

        'PAYLOAD_STAGES_LOG': os.path.join(out, 'stages.log'),
        'PAYLOAD_REPORT': os.path.join(out, 'payload-report.json'),
        'PAYLOAD_JOB_REPORT': os.path.join(out, 'jobReport.json'),
    })
    if args.input:
        env['EPICPROD_INPUT_LOCAL'] = args.input
    if handoff and resident_unit.enabled():
        # Resident units (resident_unit.py): the slot's first unit runs
        # run.sh and leaves the template; later units go straight to the
        # residents once the template is checked against run.sh's own.
        template = resident_unit.load(args.work, log)
        if template:
            return run_resident_unit(template, record, chunk, label, start, count, out, env, args)
        if not os.path.exists(resident_unit.template_path(args.work)):
            env['EPICPROD_UNIT_TEMPLATE'] = resident_unit.template_path(args.work)
    cmd = [os.path.join(args.payload, 'run.sh'), f"EVGEN/{spec['file_path']}",
           spec['ext'], str(count), chunk]
    log(f"unit {uid}: events {start}-{last} ({len(spec.get('ranges') or [rng])} ranges), chunk {label or chunk}")
    with open(os.path.join(out, 'payload.log'), 'w') as logf:
        rc = subprocess.run(cmd, cwd=run_dir(args), env=env,
                            stdout=logf, stderr=subprocess.STDOUT).returncode
    report = {}
    try:
        with open(env['PAYLOAD_REPORT']) as f:
            report = json.load(f)
    except (OSError, ValueError):
        pass
    dids = list((report.get('registration') or {}).get('dids') or [])
    # With a handoff the unit's RECO and its registration terms are the
    # harness's to merge and register at the close (run.sh, the handoff).
    handed = read_handoff(handoff) if handoff else None
    ok = rc == 0 and (dids if not handoff else handed is not None)
    record.update(rc=rc, ended_at=time.time(),
                  wall_s=round(time.time() - record['started_at'], 1),
                  events_reconstructed=(report.get('events') or {}).get('reconstructed'),
                  dids=dids if not handoff else [],
                  registration=(report.get('registration') or {}).get('outcome'),
                  handoff=handed, unit_path='run.sh',
                  status='ok' if ok else 'error',
                  message='' if ok else f'payload exit {rc}' + ('' if (dids or handed) else ', nothing registered'))
    record.update(unit_timing(out, args.work, record['started_at'], report))
    if not ok:
        # The payload's own account travels with the range's record, which
        # reaches the job report and the server: the pilot removes the
        # job directory, logs included, when the job ends.
        record['message'] += failure_detail(out)
    return uid, record, out


def run_resident_unit(template, record, chunk, label, start, count, out, env, args):
    """A unit through the slot's residents (resident_unit.py), recorded as
    run_unit records a unit of the run.sh path."""
    uid = record['unit_id']
    log(f"unit {uid}: events {start}-{start + count - 1}, chunk {label or chunk}, resident")
    try:
        handed = resident_unit.run(template, chunk, label, start, count, out, env,
                                   os.path.join(unit_tmpdir(args), f'unit-{os.getpid()}-{uid}'))
        message = ''
    except (OSError, RuntimeError, ValueError, KeyError) as error:
        handed, message = None, f'resident unit failed: {error}'
    ended = time.time()
    record.update(rc=0 if handed else 1, ended_at=ended,
                  wall_s=round(ended - record['started_at'], 1),
                  events_reconstructed=handed['events'] if handed else None,
                  dids=[], registration='deferred' if handed else None,
                  handoff=handed, unit_path='resident',
                  status='ok' if handed else 'error', message=message)
    record.update(unit_timing(out, args.work, record['started_at'], {}))
    # The stage intervals the run.sh path's report carries, from the requests.
    stamp = lambda t: time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(float(t)))  # noqa: E731
    phases = {}
    for name, first, last in (('simulation', 'started_at', 'ended_at'),
                              ('reconstruction', 'started_at', 'ended_at')):
        part = record.get(name) or {}
        if part.get(first) and part.get(last):
            phases[name] = {'started_at': stamp(part[first]), 'ended_at': stamp(part[last])}
    if phases:
        record['phases'] = phases
    return uid, record, out


def read_handoff(handoff_dir):
    """The unit's handed-off RECO and its registration terms (run.sh
    writes ``<name>.handoff.json`` beside the file), or None."""
    try:
        names = [n for n in os.listdir(handoff_dir) if n.endswith('.handoff.json')]
    except OSError:
        return None
    if not names:
        return None
    try:
        path = os.path.join(handoff_dir, names[0])
        with open(path) as f:
            h = json.load(f)
        h['handoff_file'] = path
        return h if os.path.isfile(h.get('file') or '') else None
    except (OSError, ValueError):
        return None


def failure_detail(out):
    """The failed stage line from the payload's stage log and the tail of
    its log, bounded, for the record of a failed range."""
    detail = ''
    try:
        with open(os.path.join(out, 'stages.log')) as f:
            stages = [ln.strip() for ln in f if ln.strip()]
        failed = [ln for ln in stages if ' fail ' in ln or ' decline ' in ln]
        detail += '; stage: ' + (failed[-1] if failed else (stages[-1] if stages else 'none'))
    except OSError:
        pass
    try:
        with open(os.path.join(out, 'payload.log'), errors='replace') as f:
            lines = [ln.rstrip() for ln in f.readlines()[-400:] if ln.strip()]
        # The error lines of the log's end, then its last lines: the tail
        # alone is the container's package listing when npsim dies early.
        errors = [ln for ln in lines if re.search(r'ERROR|Error on line|FATAL|Traceback|Exception|Segmentation|Killed', ln)]
        detail += '; log: ' + ' | '.join(errors[-6:] + lines[-3:])
    except OSError:
        pass
    return detail[:1500]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--work', required=True)
    ap.add_argument('--payload', required=True, help='the sandbox payload/ directory')
    ap.add_argument('--sandbox', required=True, help='the sandbox directory whose files run.sh sources its environment from')
    ap.add_argument('--input', default='', help='the staged input file; empty: the payload reads the door per range')
    ap.add_argument('--env', action='append')
    ap.add_argument('--idle-exit', type=int, default=0)
    ap.add_argument('--handoff', action='store_true',
                    help='hand each unit\'s validated RECO to the harness for the close instead of registering it')
    args = ap.parse_args()
    # The harness's switches arrive as settings: the container starts clean.
    for kv in args.env or []:
        key, _, value = kv.partition('=')
        if key.startswith('EPICPROD_SWITCH_'):
            os.environ[key] = value
    inbox = os.path.join(args.work, 'inbox')
    os.makedirs(inbox, exist_ok=True)
    os.makedirs(os.path.join(args.work, 'outbox'), exist_ok=True)
    sock = sock_path(args)

    def end_daemon(*_):
        simulation_socket = f'/tmp/essim-{os.getpid()}.sock'
        if os.path.exists(simulation_socket):
            try:
                from resident_sim import exchange
                exchange(simulation_socket, {'command': 'stop'}, 10)
            except (OSError, RuntimeError, ValueError) as error:
                log(f'resident simulation shutdown failed: {error}')
        try:
            subprocess.run(['pkill', '-f', f'managed_socket_path={sock}'], check=False)
        except OSError:
            pass

    for s in (signal.SIGTERM, signal.SIGINT):
        signal.signal(s, lambda *_: (end_daemon(), sys.exit(0)))
    log(f"slot up: work {args.work}, input {args.input}")
    idle_since = time.time()
    done = 0
    while True:
        if os.path.exists(os.path.join(inbox, 'STOP')):
            log(f"STOP after {done} units")
            break
        spec = next_unit(inbox)
        if spec is None:
            if args.idle_exit and time.time() - idle_since > args.idle_exit:
                log(f"idle {args.idle_exit} s after {done} units; exiting")
                break
            time.sleep(0.1 if resident_unit.enabled() else 1)
            continue
        uid, record, out = run_unit(spec, args)
        if out is None:
            out = os.path.join(args.work, 'outbox', uid)
            os.makedirs(out, exist_ok=True)
        with open(os.path.join(out, 'unit.json'), 'w') as f:
            json.dump(record, f)
        marker = 'done' if record.get('status') == 'ok' else 'error.json'
        with open(os.path.join(out, marker), 'w') as f:
            if marker == 'error.json':
                json.dump({'message': record.get('message'), 'rc': record.get('rc')}, f)
        os.remove(spec)
        done += 1
        idle_since = time.time()
        log(f"unit {uid}: {record.get('status')} in {record.get('wall_s')} s, dids {record.get('dids')}")
    end_daemon()
    return 0


if __name__ == '__main__':
    sys.exit(main())
