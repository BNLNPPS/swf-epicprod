#!/usr/bin/env python3
"""Resident EICrecon shared by a node's Event Service slots
(docs/EPICPROD_PAYLOAD.md, Shared resident reconstruction).

With EPICPROD_SWITCH_ES_RECO_POOL=<R> a job keeps R resident EICrecon
members for all of its slots instead of one per slot. A slot's resident
simulation and a resident EICrecon together hold about 4.4 GB, so 128 slots
with one of each would need more memory than a 512 GB Perlmutter node has
(canary 41241); reconstruction is a small fraction of a unit's time, so R
members well below the slot count keep up.

A member is a lock file, a state file and a socket. A unit takes any free
member's lock, starts the member under that lock when it is absent or dead,
sends its request, and releases the lock, so a member serves one unit at a
time and is never started twice. Members outlive the unit that started them;
the harness ends them when its slots have stopped (stop_all).

    reco_pool.py request --pool DIR --compact XML [--reply FILE] '<json request>'
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import zmq_request

POLL_S = 0.2


def size():
    """The number of members, 0 when the switch is off."""
    try:
        return max(0, int(os.environ.get('EPICPROD_SWITCH_ES_RECO_POOL') or 0))
    except ValueError:
        return 0


def enabled():
    return size() > 0


def job_pool(harness_work):
    """One pool per job, in the harness's work tree."""
    return os.path.join(os.path.abspath(harness_work), 'reco-pool')


def pool_dir(slot_work):
    """The pool of a slot's job: beside the slots (es_harness: <work>/slotNNN)."""
    return job_pool(os.path.dirname(os.path.abspath(slot_work)))


def socket_path(pool, member):
    """A Unix socket path is at most 108 bytes, so members listen in /tmp
    under a name derived from the job's pool (es_slot.sock_path)."""
    tag = hashlib.sha256(os.path.abspath(pool).encode()).hexdigest()[:12]
    return f'/tmp/esreco-{tag}-{member}.sock'


def write_json(path, value):
    temporary = Path(str(path) + '.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.replace(path)


def read_json(path):
    try:
        with open(path) as stream:
            return json.load(stream)
    except FileNotFoundError:
        return {}


def running(pid, sock):
    """The member's process is alive and is the EICrecon on this socket: a
    recycled pid or an unreaped exit is not."""
    if not pid:
        return False
    try:
        os.waitpid(int(pid), os.WNOHANG)
    except ChildProcessError:
        pass
    try:
        with open(f'/proc/{int(pid)}/stat') as stream:
            if stream.read().rsplit(')', 1)[1].split()[0] == 'Z':
                return False
        with open(f'/proc/{int(pid)}/cmdline', 'rb') as stream:
            return f'managed_socket_path={sock}'.encode() in stream.read()
    except (OSError, IndexError, ValueError):
        return False


def memory(pid):
    """The member's current memory, when /proc permits reading it."""
    try:
        with open(f'/proc/{int(pid)}/smaps_rollup') as stream:
            fields = {line.split(':')[0]: int(line.split()[1]) for line in stream
                      if line.startswith(('Rss:', 'Pss:', 'Private_Clean:', 'Private_Dirty:'))}
        return {'rss_kb': fields['Rss'], 'pss_kb': fields['Pss'],
                'private_kb': fields.get('Private_Clean', 0) + fields.get('Private_Dirty', 0)}
    except (OSError, ValueError, TypeError, KeyError):
        return None


def acquire(pool, members, prefer=0):
    """A held lock on a free member, waiting while every member is busy;
    returns (member, lock file). The search starts at the caller's preferred
    member so the slots spread over the pool."""
    os.makedirs(pool, exist_ok=True)
    while True:
        for step in range(members):
            member = (prefer + step) % members
            stream = open(os.path.join(pool, f'{member}.lock'), 'a')
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return member, stream
            except BlockingIOError:
                stream.close()
        time.sleep(POLL_S)


def member_log(pool, member):
    """A member's log: beside the slots' logs when the work tree is in node
    memory (EPICPROD_ES_POOL_LOG_DIR, es_harness), else in the pool."""
    d = os.environ.get('EPICPROD_ES_POOL_LOG_DIR') or pool
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f'{member}.log')


def start(pool, member, compact, startup_s):
    """Start the member's EICrecon, as run.sh starts a slot's own, and wait
    for its socket. Returns its state."""
    sock = socket_path(pool, member)
    if os.path.exists(sock):
        os.unlink(sock)                     # a dead member's socket
    began = time.time()
    log = open(member_log(pool, member), 'ab')
    child = subprocess.Popen(
        ['eicrecon', f'-Pdd4hep:xml_files={compact}', f'-Ppodio:managed_socket_path={sock}',
         '-Pjana:warmup_timeout=0', '-Pjana:timeout=0'],
        stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    log.close()
    deadline = time.monotonic() + startup_s
    while not os.path.exists(sock):
        if child.poll() is not None:
            raise RuntimeError(f'reco pool member {member} exited {child.returncode} before its socket appeared; '
                               f'see {member_log(pool, member)}')
        if time.monotonic() >= deadline:
            child.kill()
            raise RuntimeError(f'reco pool member {member} gave no socket in {startup_s} s')
        time.sleep(POLL_S)
    state = {'member': member, 'pid': child.pid, 'socket': sock, 'compact': compact,
             'initialization_started_at': began, 'initialized_at': time.time(), 'requests': 0}
    write_json(os.path.join(pool, f'{member}.json'), state)
    return state


def request(pool, payload, compact, record=None, prefer=0, timeout_s=600, startup_s=600):
    """One reconstruction by a free member; returns EICrecon's reply with the
    pool's timing. ``started_at``/``ended_at`` bound the request to the member
    alone; ``pool_wait_s`` is the wait for a free member before it; the
    member's initialization timestamps are present only when this request
    started it. With ``record`` the reply file carries ``request_started_at``
    while the request runs and the whole reply after, as zmq_request writes."""
    members = size()
    if members <= 0:
        raise RuntimeError('the reco pool switch is off')
    asked = time.time()
    member, lock = acquire(pool, members, prefer)
    try:
        acquired = time.time()
        state = read_json(os.path.join(pool, f'{member}.json'))
        restarted = None
        if not running(state.get('pid'), state.get('socket')):
            if state.get('pid'):
                restarted = f"member {member} (pid {state['pid']}) was not running"
            state = start(pool, member, compact, startup_s)
            started_member = True
        else:
            if state.get('compact') != compact:
                raise RuntimeError(f'reco pool member {member} serves {state["compact"]}, not {compact}')
            started_member = False
        sent = time.time()
        if record:
            write_json(record, {'request_started_at': sent})
        try:
            reply = zmq_request.request(state['socket'], payload, timeout_s)
        except RuntimeError:
            # A member that does not answer would hold every later request
            # to its timeout: it is ended here, under its lock, and the next
            # unit to take it starts it afresh.
            try:
                os.killpg(int(state['pid']), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            raise
        ended = time.time()
        state['requests'] = int(state.get('requests') or 0) + 1
        state['memory'] = memory(state['pid'])
        write_json(os.path.join(pool, f'{member}.json'), state)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()
    reply.update(request_started_at=sent, request_ended_at=ended,
                 pool={'member': member, 'pid': state['pid'], 'asked_at': asked, 'acquired_at': acquired,
                       'wait_s': round(acquired - asked, 3), 'requests': state['requests'],
                       'memory': state['memory'], 'started': started_member})
    if restarted:
        reply['pool']['restarted'] = restarted
    if started_member:
        reply['pool'].update(initialization_started_at=state['initialization_started_at'],
                             initialized_at=state['initialized_at'])
    if record:
        write_json(record, reply)
    return reply


def stop_all(pool, grace_s=10):
    """End every member of the job's pool (the harness, after its slots);
    returns how many were running."""
    members = []
    for path in Path(pool).glob('*.json'):
        state = read_json(path)
        if running(state.get('pid'), state.get('socket')):
            members.append(state)
            try:
                os.killpg(int(state['pid']), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
    ended = len(members)
    deadline = time.monotonic() + grace_s
    while members and time.monotonic() < deadline:
        members = [m for m in members if running(m['pid'], m['socket'])]
        time.sleep(POLL_S)
    for state in members:
        try:
            os.killpg(int(state['pid']), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    return ended


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    ask = sub.add_parser('request')
    ask.add_argument('--pool', required=True)
    ask.add_argument('--compact', required=True)
    ask.add_argument('--reply')
    ask.add_argument('--prefer', type=int, default=0)
    ask.add_argument('payload')
    args = parser.parse_args()
    reply = request(args.pool, json.loads(args.payload), args.compact, args.reply, args.prefer,
                    startup_s=int(os.environ.get('EPICPROD_RECO_STARTUP_S') or 600))
    print(json.dumps(reply), flush=True)


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, ValueError) as error:
        print(f'ERROR: {error}', file=sys.stderr, flush=True)
        sys.exit(1)
