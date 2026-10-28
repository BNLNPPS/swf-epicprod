#!/usr/bin/env python3
"""Resident Event Service units without a run.sh per unit
(docs/EPICPROD_PAYLOAD.md, Resident Event Service units).

With EPICPROD_SWITCH_ES_RESIDENT_UNIT=1 a slot's first unit runs the payload
as before and leaves the unit template beside the slot's work: the arguments
of its resident simulation request, the names and seed run.sh derived for its
chunk, and the registration terms of its handoff. Everything but the event
interval (an EVGEN unit's skip with it), the random seed and the output names
is constant for the job, so a later unit runs as one request to the slot's
resident simulation, one to its resident EICrecon (or the job's shared one,
reco_pool.py) and a handoff to the close. The environment, landing check,
gun steering, ROOT opens and per-stage reports of run.sh are not repeated.

The slot uses a template only after recomputing the names and seed of the
unit that wrote it and finding run.sh's own; otherwise every unit keeps the
run.sh path. Counts come from the residents' replies: the completed
simulation's events and EICrecon's events processed. The close counts its
merged file against the units' sum (es_close.sh), so each count is checked
where its output is saved.
"""
import json
import os
from pathlib import Path
import shutil
import time
from types import SimpleNamespace

import reco_pool
import resident_sim
import zmq_request

TEMPLATE = 'unit-template.json'
SEED_MODULUS = 2147483647           # npsim's positive 31-bit seed (run.sh, the particle gun's seed)
_loaded = {}


def enabled():
    return os.environ.get('EPICPROD_SWITCH_ES_RESIDENT_UNIT') == '1'


def template_path(work):
    return os.path.join(work, TEMPLATE)


def identity(template, chunk, label, skip):
    """run.sh's output name and seed for a chunk (Argument parsing and the
    particle gun's seed), from the constants its template recorded."""
    task = template['task']
    taskname = template['taskname'][:len(template['taskname']) - len(task)] + '.' + (label or chunk)
    if template.get('gun_stem_hash'):
        offset = skip if template.get('gun_events') else int(chunk, 10)
        seed = (int(template['gun_stem_hash'], 16) * 10000 + offset + 1) % SEED_MODULUS
    else:
        seed = int(chunk, 10) + 1
    return taskname, max(seed, 1)


def flag_values(argv):
    """The value after each flag of an npsim argument list."""
    return {flag: value for flag, value in zip(argv, argv[1:]) if flag.startswith('--')}


def check(template, input_local=None):
    """Why the template cannot stand in for run.sh, or None. An EVGEN
    template stands in only over the job's staged input: a unit whose input
    run.sh makes itself (a background merge) keeps run.sh."""
    if template.get('version') != 1:
        return f"template version {template.get('version')}"
    taskname, seed = identity(template, template['chunk'], template.get('label') or '', template['skip'])
    if (taskname, seed) != (template['taskname'], template['seed']):
        return f"recomputed {taskname} seed {seed}, run.sh had {template['taskname']} seed {template['seed']}"
    values = flag_values(template['npsim_args'])
    expected = {'--random.seed': str(seed), '--numberOfEvents': str(template['events'])}
    if '--meta.eventNumberOffset' in values:
        expected['--meta.eventNumberOffset'] = str(template['skip'])
    evgen = '--inputFiles' in values
    if evgen:
        expected['--skipNEvents'] = str(template['skip'])
    wrong = [flag for flag, value in expected.items() if values.get(flag) != value]
    if wrong:
        return 'request arguments differ at ' + ', '.join(wrong)
    if not values.get('--outputFile', '').endswith('/' + taskname + '.edm4hep.root'):
        return 'request output name differs'
    if evgen:
        if '--enableGun' in template['npsim_args']:
            return 'both a particle gun and an input file'
        if not input_local or os.path.realpath(values['--inputFiles']) != os.path.realpath(input_local):
            return 'the input is not the job\'s staged input'
    elif '--enableGun' not in template['npsim_args'] or '--steeringFile' not in values:
        return 'not a particle-gun request'
    if template['full_events'] != template['events'] or template['reco_events'] != template['events']:
        return 'the template unit did not reconstruct every event'
    return None


def load(work, log, input_local=None):
    """The slot's checked template, or None (the run.sh path)."""
    path = template_path(work)
    if path in _loaded:
        return _loaded[path]
    try:
        with open(path) as stream:
            template = json.load(stream)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        template, reason = None, f'unreadable: {error}'
    else:
        try:
            reason = check(template, input_local)
        except (KeyError, TypeError, ValueError) as error:
            reason = f'incomplete: {error}'
    if reason:
        log(f'unit template refused ({reason}); every unit keeps the run.sh path')
        template = None
    else:
        log(f"unit template accepted from {template['taskname']}: later units go straight to the residents")
    _loaded[path] = template
    return template


def arguments(template, values, steering):
    """The template's request with this unit's values, the resident's copy of
    the steering (run.sh's copy is gone with its scratch)."""
    argv, out, index = template['npsim_args'], [], 0
    while index < len(argv):
        flag = argv[index]
        if flag in values or flag == '--steeringFile':
            out += [flag, str(values.get(flag, steering))]
            index += 2
        else:
            out.append(flag)
            index += 1
    return out


def alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def asynchronous():
    """EPICPROD_SWITCH_ES_RECO_ASYNC=1: a unit's reconstruction and handoff
    finish in the background through the job's shared EICrecon while the slot
    simulates its next unit (EPICPROD_PAYLOAD.md, Asynchronous
    reconstruction)."""
    return os.environ.get('EPICPROD_SWITCH_ES_RECO_ASYNC') == '1'


def handoff_in_place(env):
    """EPICPROD_SWITCH_ES_HANDOFF_IN_PLACE=1: a unit's RECO is written in its
    handoff directory, so the handoff moves no file between directories. On
    Perlmutter's scratch from 28 October the handoff, whose one such move this
    removes, was served one unit at a time across the node, 6 to 23 s each, and
    a node's units waited for it in turn, up to 88 minutes (jobs 6289946 to
    6314067)."""
    return env.get('EPICPROD_SWITCH_ES_HANDOFF_IN_PLACE') == '1' and bool(env.get('EPICPROD_RECO_HANDOFF'))


def simulate(template, chunk, label, start, count, out, env, scratch):
    """The unit's simulation through the slot's resident npsim; returns what
    reconstruct() takes. Raises on a failed step, with the reason, and then
    removes the unit's scratch."""
    out, scratch = Path(out), Path(scratch)
    skip = start - 1
    taskname, seed = identity(template, chunk, label, skip)
    full = scratch / template['full_dir'] / f'{taskname}.edm4hep.root'
    if handoff_in_place(env):
        # The RECO is written where the harness takes it: the handoff moves
        # nothing between directories (handoff_in_place).
        reco = Path(env['EPICPROD_RECO_HANDOFF']) / f'{taskname}.eicrecon.edm4eic.root'
    else:
        reco = scratch / template['reco_dir'] / f'{taskname}.eicrecon.edm4eic.root'
    full.parent.mkdir(parents=True, exist_ok=True)
    reco.parent.mkdir(parents=True, exist_ok=True)
    state = Path(env['EPICPROD_SIM_STATE'])
    values = {'--random.seed': seed, '--numberOfEvents': count, '--outputFile': full}
    if '--meta.eventNumberOffset' in template['npsim_args']:
        values['--meta.eventNumberOffset'] = skip
    if '--inputFiles' in template['npsim_args']:
        values['--skipNEvents'] = skip
    try:
        resident_sim.request(SimpleNamespace(
            socket=env['EPICPROD_SIM_SOCKET'], state_dir=str(state), reply=out / 'simulation.json',
            startup_timeout=600, request_timeout=1800,
            source_id=env.get('EPICPROD_EVGEN_SOURCE_ID'),
            npsim_args=arguments(template, values, str(state / 'gun.steer'))))
        with open(out / 'simulation.json') as stream:
            simulated = json.load(stream)
        if simulated.get('events') != count or not full.is_file():
            raise RuntimeError(f"simulation answered {simulated.get('events')} events of {count}")
    except BaseException:
        shutil.rmtree(scratch, ignore_errors=True)
        raise
    return {'taskname': taskname, 'full': full, 'reco': reco, 'count': count, 'scratch': scratch}


def reconstruct(template, unit, out, env):
    """A simulated unit's reconstruction and handoff; returns the handoff
    record and removes the unit's scratch. Raises on a failed step."""
    out, full, reco, count = Path(out), unit['full'], unit['reco'], unit['count']
    taskname = unit['taskname']
    try:
        asked = {'input_file': str(full), 'output_file': str(reco), 'nskip': 0,
                 'nevents': count, 'run_id': taskname}
        if env.get('EPICPROD_RECO_POOL'):
            # The job's shared EICrecon (reco_pool.py): any free member,
            # started or restarted by this unit when it must be.
            reply = reco_pool.request(env['EPICPROD_RECO_POOL'], asked,
                                      flag_values(template['npsim_args'])['--compactFile'],
                                      out / 'reconstruction.json',
                                      prefer=int(env.get('EPICPROD_RECO_POOL_PREFER') or 0))
        else:
            # A dead EICrecon leaves its socket path behind, and a request to
            # it would wait out the reply timeout.
            with open(env['EPICPROD_RECO_STATE']) as stream:
                reco_pid = json.load(stream).get('pid')
            if not alive(reco_pid) or not os.path.exists(env['EPICPROD_RECO_SOCKET']):
                raise RuntimeError(f'the resident EICrecon (pid {reco_pid}) is not running')
            started = time.time()
            resident_sim.write_json(out / 'reconstruction.json', {'request_started_at': started})
            reply = zmq_request.request(env['EPICPROD_RECO_SOCKET'], asked)
            reply.update(request_started_at=started, request_ended_at=time.time())
            resident_sim.write_json(out / 'reconstruction.json', reply)
        if reply.get('status') != 'completed':
            raise RuntimeError(f"reconstruction {reply.get('status')}: {reply.get('error') or reply}")
        events = reply.get('events_processed')
        if events != count or not reco.is_file() or reco.stat().st_size == 0:
            raise RuntimeError(f'reconstruction processed {events} events of {count}')
        handoff = Path(env['EPICPROD_RECO_HANDOFF'])
        handoff.mkdir(parents=True, exist_ok=True)
        moved = handoff / reco.name
        if reco != moved:
            shutil.move(str(reco), str(moved))
        terms = dict(template['handoff'], file=str(moved), events=events)
        resident_sim.write_json(handoff / f'{taskname}.handoff.json', terms)
        return dict(terms, handoff_file=str(handoff / f'{taskname}.handoff.json'))
    except BaseException:
        # A failed unit's RECO written in place is not left for the close.
        if handoff_in_place(env):
            Path(reco).unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(unit['scratch'], ignore_errors=True)


def run(template, chunk, label, start, count, out, env, scratch):
    """One unit through the residents and into the handoff; returns the
    handoff record. Raises on any failed step, with the reason."""
    return reconstruct(template, simulate(template, chunk, label, start, count, out, env, scratch), out, env)
