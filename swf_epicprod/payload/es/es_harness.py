#!/usr/bin/env python3
"""The node harness's front end (docs/NODE_EVENT_DISPATCHER.md): takes
event ranges from the pilot's channel, keeps N slots fed through their
inboxes, reports each range's outcome to the pilot, and owns the
deadline. It runs where the pilot runs an Event Service payload, outside
the task's image; each slot is one long-lived container running
es_slot.py, with its own resident EICrecon.

    es_harness.py --slots N --work <dir> --sandbox <dir> --image <image>
                  --csv-base <manifest base> --stamp <stamp>
                  [--channel <yampl name>] [--ranges-file <json>]
                  [--input <staged file>] [--deadline-s S --margin-s M]
                  [--env KEY=VALUE ...] [--summary <json>]

The channel is the pilot's yampl socket (PILOT_EVENTRANGECHANNEL when not
given); --ranges-file feeds a list of range dicts instead, the loopback
without a pilot. A unit is one range, or with --events-per-unit K (a
fine-grained task, one event per range) up to K consecutive ranges of
one block of K events, run as one chunk of the row and reported range by
range after; K is the loss quantum, the block's index names the chunk.
With --close-s S (ES_CLOSE_S) the units hand their validated RECO to the
harness instead of registering it, and every S seconds, and at the end,
the harness merges the units since the last close into one podio file
and registers it (es_close.sh in the image: the Package of the design);
a unit's ranges are reported to the pilot only once its close stands,
so a close that fails sends its events back to the server. The
deadline's margin must cover the last close. The input file is the pilot's staged copy of the range's
LFN in the job directory (the parent of the working directory), or
--input. The deadline is seconds of wall since start; at deadline minus
margin no further range is taken, the ranges in flight finish and are
reported, and the harness exits: the ranges it never took go back to the
server untouched.

Reports to the pilot are its own forms: "<receipt>,ID:<range>,CPU:<s>,WALL:<s>"
for a finished range (the receipt is the unit record; the science
outputs went to the catalog from the payload), "ERR_ATHENAMP_PROCESS
<range>: <why>" for a failed one.
"""
import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from call_home import CallHome, default_flag, load_job_env, status_key  # noqa: E402
from es_record import RecordOut, trim_unit  # noqa: E402


def meminfo_kb(field):
    """One field of /proc/meminfo in kB, or None."""
    try:
        with open('/proc/meminfo') as f:
            for line in f:
                if line.startswith(field + ':'):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def log(msg):
    print(f"[es_harness {time.strftime('%H:%M:%S')}] {msg}", flush=True)


class ChannelFeed:
    """Ranges from the pilot over yampl, one per ask."""

    def __init__(self, name):
        import yampl
        self.sock = yampl.ClientSocket(name, 'local')
        self.exhausted = False

    def ask(self):
        if self.exhausted:
            return None
        self.sock.send_raw(b"Ready for events")
        while True:
            size, buf = self.sock.try_recv_raw()
            if size != -1:
                break
            time.sleep(0.01)                # the pilot's own message cadence (esmessage.py)
        message = buf.decode('utf8') if isinstance(buf, bytes) else str(buf)
        if "No more events" in message:
            self.exhausted = True
            return None
        ranges = json.loads(message)
        if isinstance(ranges, dict):
            ranges = [ranges]
        return ranges[0] if ranges else None

    def report(self, text):
        self.sock.send_raw(text.encode('utf8'))


class FileFeed:
    """Ranges from a JSON list, for the loopback; reports are logged."""

    def __init__(self, path):
        with open(path) as f:
            self.ranges = list(json.load(f))
        self.exhausted = False
        self.reports = []

    def ask(self):
        if not self.ranges:
            self.exhausted = True
            return None
        return self.ranges.pop(0)

    def report(self, text):
        self.reports.append(text)
        log(f"report: {text[:160]}")


def container_command(args, inner, extra_binds=()):
    """The task's image run over the sandbox and the work tree, as the
    slots and the close run it."""
    runtime = shutil.which('apptainer') or shutil.which('singularity') or \
        '/cvmfs/atlas.cern.ch/repo/containers/sw/apptainer/x86_64-el8/current/bin/apptainer'
    cmd = [runtime, 'exec', '--cleanenv', '-B', args.sandbox, '-B', args.work]
    for b in extra_binds:
        cmd += ['-B', b]
    if os.path.isdir('/cvmfs'):
        cmd += ['-B', '/cvmfs']
    cmd += ['--pwd', args.sandbox, args.image, '/bin/bash', '-c', inner]
    return cmd


def kill_group(proc):
    """SIGKILL a process started in its own session, with everything under it."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


class Close:
    """One close: the pending units' RECO merged and registered by
    es_close.sh in the image, in the background; the units' ranges are
    reported when it ends."""

    def __init__(self, index, args, units):
        self.index = index
        self.units = units                      # [(slot, uid, record, range_ids)]
        self.dir = os.path.join(args.work, 'closes', f'{index:04d}')
        os.makedirs(self.dir, exist_ok=True)
        first = units[0][2]['handoff']
        self.name = f"{first['name']}.{os.environ.get('PANDAID') or 'loopback'}.{index:04d}.eicrecon.edm4eic.root"
        handoffs = [u[2]['handoff']['handoff_file'] for u in units]
        inner = (f"cd {args.sandbox} && bash {args.payload}/es/es_close.sh {self.dir} {self.name} "
                 + " ".join(handoffs))
        self.started = time.time()
        self.proc = subprocess.Popen(container_command(args, inner),
                                     stdout=open(os.path.join(self.dir, 'container.log'), 'w'),
                                     stderr=subprocess.STDOUT, start_new_session=args.preempt_at_s > 0)
        log(f"close {index}: {len(units)} unit(s), {sum(int(u[2]['handoff'].get('events') or 0) for u in units)} events -> {self.name} (pid {self.proc.pid})")

    def result(self):
        """The close's record when it has ended, else None."""
        if self.proc.poll() is None:
            return None
        rec = {'index': self.index, 'name': self.name, 'units': [u[1] for u in self.units],
               'rc': self.proc.returncode, 'wall_s': round(time.time() - self.started, 1),
               'started_at': self.started, 'ended_at': time.time()}
        try:
            with open(os.path.join(self.dir, 'close.json')) as f:
                rec.update(json.load(f))
        except (OSError, ValueError):
            rec.setdefault('outcome', 'failed')
            rec.setdefault('message', f'close exited {self.proc.returncode} without a record')
        rec['ok'] = self.proc.returncode == 0 and rec.get('outcome') in ('registered', 'pending')
        return rec


class Slot:
    def __init__(self, index, args, input_path):
        self.index = index
        self.work = os.path.join(args.work, f'slot{index:03d}')
        self.inbox = os.path.join(self.work, 'inbox')
        self.outbox = os.path.join(self.work, 'outbox')
        os.makedirs(self.inbox, exist_ok=True)
        os.makedirs(self.outbox, exist_ok=True)
        self.unit = None            # the unit id in flight
        self.range_ids = []
        self.taken_at = None
        runtime = shutil.which('apptainer') or shutil.which('singularity') or \
            '/cvmfs/atlas.cern.ch/repo/containers/sw/apptainer/x86_64-el8/current/bin/apptainer'
        inner = (f"cd {args.sandbox} && python3 {args.payload}/es/es_slot.py "
                 f"--work {self.work} --payload {args.payload} --sandbox {args.sandbox} "
                 + (f" --input {input_path}" if input_path else "")
                 + (" --handoff" if args.close_s > 0 else "")
                 + "".join(f" --env {kv}" for kv in (args.env or [])))
        cmd = container_command(args, inner, extra_binds=[os.path.dirname(input_path)] if input_path else [])
        self.log = open(os.path.join(self.work, 'slot.log'), 'w')
        self.proc = subprocess.Popen(cmd, stdout=self.log, stderr=subprocess.STDOUT,
                                     start_new_session=args.preempt_at_s > 0)
        log(f"slot {index} started (pid {self.proc.pid})")

    def free(self):
        return self.unit is None and self.proc.poll() is None

    def give(self, spec):
        uid = spec['unit_id']
        self.range_ids = [r['eventRangeID'] for r in spec['ranges'] if not r.get('replay')]
        tmp = os.path.join(self.inbox, f'.{uid}.tmp')
        with open(tmp, 'w') as f:
            json.dump(spec, f)
        os.rename(tmp, os.path.join(self.inbox, f'{uid}.unit.json'))
        self.unit, self.taken_at = uid, time.time()
        self.unit_events = len(spec['ranges'])
        self.unit_range = spec['range']

    def result(self):
        """(unit id, record, ok) when the unit in flight has finished."""
        if self.unit is None:
            return None
        out = os.path.join(self.outbox, self.unit)
        done, err = os.path.join(out, 'done'), os.path.join(out, 'error.json')
        if not (os.path.exists(done) or os.path.exists(err)):
            if self.proc.poll() is not None:
                return self.unit, {'message': f'slot exited {self.proc.returncode} with the unit in flight'}, False
            return None
        record = {}
        try:
            with open(os.path.join(out, 'unit.json')) as f:
                record = json.load(f)
        except (OSError, ValueError):
            pass
        uid = self.unit
        self.unit = None
        return uid, record, os.path.exists(done)

    def stop(self):
        with open(os.path.join(self.inbox, 'STOP'), 'w'):
            pass

    def kill(self):
        """TEST AND DEMO ONLY (preemption): the slot's container and all it
        runs, gone at once, as on a node that is taken away."""
        kill_group(self.proc)

    def wait(self, timeout):
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
        self.log.close()


class Pool:
    """The job's ranges as the pilot hands them, gathered in the
    background and kept in event order: the server hands them in no
    particular order within a fetch (job 3556341 got 6 8 10 3 15 4 ...),
    and the pilot fetches a few at a time on its own cadence (two a
    fetch every half minute for a one-core job, job 3556537), so a unit
    is cut as soon as its block is whole rather than after every range
    has arrived."""

    def __init__(self, feed, lookahead=0, expected=0):
        """lookahead: the most ranges held before the harness asks for them
        (0 = no limit). The pilot kills the payload 30 minutes after it
        answers "No more events" (pilot esprocess.py waiting_time), so a pool
        that drains the pilot at the start is killed before its last close
        reports (job 3618786: 6,000 ranges pooled, SIGTERM 30 min later,
        nothing credited). With a lookahead the last ranges are asked for
        only as slots free up, and "No more events" comes near the end."""
        self.feed = feed
        self.lookahead = lookahead
        self.expected = expected            # loop mode: stop asking after this many ranges
        self.received = 0
        self.complete = False               # every expected range arrived, "No more events" never asked
        self.ranges = []
        self.cut_count = {}                 # (LFN, block) -> events of that block already cut
        self.lock = threading.Lock()
        self.wanted = threading.Event()     # a free slot found no whole block
        self.thread = threading.Thread(target=self._gather, name='pool', daemon=True)
        self.thread.start()

    def want(self):
        """Ask past the lookahead: a slot is free and no block is whole."""
        self.wanted.set()

    def _gather(self):
        while not self.feed.exhausted:
            if self.expected and self.received >= self.expected:
                # The last range of the file is here; asking again would draw
                # "No more events" and start the pilot's 30-minute kill clock.
                self.complete = True
                break
            if self.lookahead and len(self) >= self.lookahead and not self.wanted.is_set():
                self.wanted.wait(0.5)
                continue
            rng = self.feed.ask()
            self.wanted.clear()
            if rng is None:
                break
            with self.lock:
                self.received += 1
                self.ranges.append(rng)
                self.ranges.sort(key=lambda r: (r.get('LFN', ''), int(r['startEvent'])))

    @property
    def exhausted(self):
        return (self.feed.exhausted or self.complete) and not self.thread.is_alive()

    def __len__(self):
        with self.lock:
            return len(self.ranges)

    def first(self):
        """The first pooled range, or None; the pool is left as it is."""
        with self.lock:
            return dict(self.ranges[0]) if self.ranges else None

    def clear(self):
        with self.lock:
            self.ranges = []

    def take_unit(self, per_unit, min_events=0, max_events=0):
        """The ranges of the next unit: one range, or up to per_unit
        consecutive ranges within one block of per_unit events (block b
        is events b*K+1 to (b+1)*K), so that a unit is a chunk of the row
        and its block index names its outputs. A block is cut when it is
        whole, or when no more ranges are coming; empty otherwise.
        max_events caps the unit (what fits before the deadline), and a
        run that reaches the cap is cut at once."""
        with self.lock:
            if not self.ranges:
                return []
            if per_unit < 1:
                return [self.ranges.pop(0)]
            # The pool is read as runs: consecutive ranges of one block, in
            # event order. The first run that can be cut is cut; a run that
            # cannot yet is passed over, never left to hold up the runs behind
            # it (jobs 3618888 and 3618931: one short run at the head, its
            # block's other events already taken, idled every slot for the
            # rest of the job or until the file ran out).
            i = 0
            while i < len(self.ranges):
                first = self.ranges[i]
                block = (int(first['startEvent']) - 1) // per_unit
                members = [first]
                for rng in self.ranges[i + 1:]:
                    start = int(rng['startEvent'])
                    if (rng.get('LFN') != first.get('LFN') or start != int(members[-1]['lastEvent']) + 1
                            or (start - 1) // per_unit != block or len(members) >= per_unit):
                        break
                    members.append(rng)
                run = len(members)
                key = (first.get('LFN'), block)
                # A run is cut when its block is whole; when it is all that is
                # left of its block (every other event of it already cut, so
                # nothing more of it can come, however short or late it is:
                # job 3618931's event 5984 arrived after 5751-5983 and
                # 5985-6000 were cut); when no more ranges are coming; when it
                # reaches the cap (what fits before the deadline); or, streaming,
                # when a free slot can start on at least min_events of it. A
                # block whose first events are still coming is not whole for
                # reaching its last (job 3556539 cut events 2-5 and then 1 alone).
                rest = run + self.cut_count.get(key, 0) >= per_unit
                capped = 0 < max_events <= run
                if capped:
                    members = members[:max_events]
                if (run >= per_unit or rest or capped or self.exhausted
                        or (min_events > 0 and run >= min_events)):
                    del self.ranges[i:i + len(members)]
                    self.cut_count[key] = self.cut_count.get(key, 0) + len(members)
                    return members
                i += run
            return []


def take_unit(pool, per_unit):
    """The next unit off a plain list (the tests' form of the pool)."""
    if not pool:
        return []
    members = [pool.pop(0)]
    if per_unit < 1:
        return members
    first = members[0]
    block = (int(first['startEvent']) - 1) // per_unit
    while pool and len(members) < per_unit:
        rng, prev = pool[0], members[-1]
        start = int(rng['startEvent'])
        if (rng.get('LFN') != first.get('LFN') or start != int(prev['lastEvent']) + 1
                or (start - 1) // per_unit != block):
            break
        members.append(pool.pop(0))
    return members


def seconds_per_event(samples):
    """A unit's seconds per event on this node over the finished units'
    (events, wall s), or None before any has finished. Rough on purpose:
    the drain it sizes needs minutes, not seconds."""
    events = sum(n for n, w in samples if n > 0 and w > 0)
    return sum(w for n, w in samples if n > 0 and w > 0) / events if events else None


def replay_unit(template, state, per_unit, expected):
    """TEST AND DEMO ONLY: the next block of events again, as ranges marked
    replay (never reported to the pilot). Cycles over the file's events."""
    start = state['next']
    last = min(start + per_unit - 1, expected)
    members = [dict(template, eventRangeID=f"replay{state['pass']}-{e}", startEvent=e, lastEvent=e, replay=True)
               for e in range(start, last + 1)]
    state['next'] = last + 1
    if state['next'] > expected:
        state['next'], state['pass'] = 1, state['pass'] + 1
    return members


def unit_spec(members, per_unit, file_path, ext, stamp):
    """The unit the slot runs: its span as 'range' (the first range's id
    as the unit id), its member ranges, and the block that names it."""
    first, last = members[0], members[-1]
    start, end = int(first['startEvent']), int(last['lastEvent'])
    span = dict(first, startEvent=start, lastEvent=end)
    spec = {'contract_version': 1, 'unit_id': first['eventRangeID'], 'range': span,
            'ranges': members, 'file_path': file_path, 'ext': ext, 'stamp': stamp}
    if per_unit > 0:
        spec['block'], spec['unit_events'] = (start - 1) // per_unit, per_unit
    return spec


def find_input(job_dir, lfn):
    for base in (job_dir, os.getcwd()):
        p = os.path.join(base, lfn)
        if os.path.isfile(p):
            return p
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--slots', type=int, default=int(os.environ.get('ES_SLOTS', '1')))
    ap.add_argument('--work', required=True)
    ap.add_argument('--sandbox', default=os.getcwd())
    ap.add_argument('--payload', default=None)
    ap.add_argument('--image', default=os.environ.get('ES_PAYLOAD_IMAGE', ''))
    ap.add_argument('--csv-base', required=True)
    ap.add_argument('--stamp', required=True)
    ap.add_argument('--channel', default=os.environ.get('PILOT_EVENTRANGECHANNEL', ''))
    ap.add_argument('--ranges-file')
    ap.add_argument('--input')
    ap.add_argument('--deadline-s', type=float, default=float(os.environ.get('ES_DEADLINE_S', '0')))
    ap.add_argument('--margin-s', type=float, default=float(os.environ.get('ES_MARGIN_S', '1800')))
    ap.add_argument('--env', action='append')
    ap.add_argument('--summary', default='es_summary.json')
    ap.add_argument('--events-per-unit', type=int, default=int(os.environ.get('ES_EVENTS_PER_UNIT', '0')),
                    help='consecutive events per unit for a fine-grained task (one event per range); 0 = one range per unit')
    ap.add_argument('--close-s', type=float, default=float(os.environ.get('ES_CLOSE_S', '0')),
                    help='seconds between closes: the units since the last close merged into one file and registered; 0 = each unit registers its own')
    ap.add_argument('--min-unit-events', type=int, default=int(os.environ.get('ES_MIN_UNIT_EVENTS', '16')),
                    help='a free slot starts on this many contiguous events rather than wait for its whole block')
    ap.add_argument('--loop', action='store_true', default=os.environ.get('ES_LOOP') == '1',
                    help='TEST AND DEMO ONLY: once every range of the file has arrived, replay '
                         'the same events until the deadline margin, so the slots work to the wall; '
                         'replays are saved in closes and never reported to the pilot')
    ap.add_argument('--preempt-at-s', type=float, default=float(os.environ.get('ES_PREEMPT_AT_S', '0')),
                    help='TEST AND DEMO ONLY: a sudden end at this many seconds, as on a preempted '
                         'node: every slot and a running close killed at once, nothing more closed '
                         'or reported; the units cut off and the units whose output never left the '
                         'node are recorded as what was lost')
    ap.add_argument('--expected-events', type=int, default=int(os.environ.get('ES_EXPECTED_EVENTS', '0')),
                    help='with --loop: the events the file holds, after which no more ranges are asked for')
    args = ap.parse_args()
    args.sandbox = os.path.abspath(args.sandbox)
    args.work = os.path.abspath(args.work)
    args.payload = os.path.abspath(args.payload or os.path.join(args.sandbox, 'payload'))
    if not args.image:
        log("ERROR: no image (--image or ES_PAYLOAD_IMAGE)")
        return 2
    import csv
    with open(os.path.join(args.sandbox, f'{args.csv_base}.csv')) as f:
        row = next(csv.reader(f))
    file_path, ext = row[0], row[1]
    feed = FileFeed(args.ranges_file) if args.ranges_file else ChannelFeed(args.channel)
    started = time.time()
    os.makedirs(args.work, exist_ok=True)
    slots, summary = [], {'stamp': args.stamp, 'slots': args.slots, 'image': args.image,
                          'done': [], 'failed': [], 'closes': [], 'untaken_at_deadline': False,
                          'interrupted': [], 'unshipped': [],
                          'started_at': started}
    input_path = args.input
    # Just in time: a unit ready for a quarter of the slots, the rest pulled
    # as slots free (a pilot fetch is 2 x cores ranges, one round trip). The
    # job streams: ranges pulled, processed and reported continuously, so
    # when the server runs dry only the units in flight and the last close
    # remain, well inside the pilot's 30 minutes after "No more events".
    lookahead = int(os.environ.get('ES_POOL_LOOKAHEAD')
                    or max(1, args.slots // 4) * max(1, args.events_per_unit))
    if args.loop and not (args.deadline_s and args.expected_events):
        log("loop mode needs --deadline-s and --expected-events; running without it")
        args.loop = False
    pool = Pool(feed, lookahead=lookahead, expected=args.expected_events if args.loop else 0)
    template = {}                                          # a real range, the replays' model
    replay = {'pass': 1, 'next': 1}
    pending, closes, last_close = [], [], time.time()     # units handed over, awaiting a close
    taking = True                                          # False past the deadline's margin
    log(f"harness up: {args.slots} slots, image {args.image}, row {row[:2]}, "
        f"unit {args.events_per_unit or 'one range'} events, "
        f"close every {args.close_s or 'unit'} s, "
        f"deadline {args.deadline_s or 'none'} s, margin {args.margin_s} s")

    reported = [0]                                          # reports sent since the harness last waited on the pilot
    unit_walls = []                                         # (events, wall s) of the finished units

    def status():
        # The job's state for call home: read from this loop's variables,
        # which a closure sees as they stand when it is called.
        in_flight_now = [s for s in list(slots) if s.unit is not None]
        closed = list(summary['closes'])
        return {
            'kind': 'es_harness', 'host': os.uname().nodename, 'stamp': args.stamp,
            'cores': len(os.sched_getaffinity(0)), 'loadavg': os.getloadavg(),
            'mem_available_kb': meminfo_kb('MemAvailable'),
            'elapsed_s': round(time.time() - started), 'deadline_s': args.deadline_s,
            'margin_s': args.margin_s, 'taking': taking,
            'slots_requested': args.slots, 'slots_started': len(slots),
            'units_in_flight': len(in_flight_now),
            'unit_oldest_s': round(max((time.time() - (s.taken_at or time.time()) for s in in_flight_now), default=0)),
            'units_done': len(summary['done']), 'units_failed': len(summary['failed']),
            'units_awaiting_close': len(pending), 'closes': len(closed),
            'events_closed': sum(c.get('events') or 0 for c in closed if c.get('ok')),
            'ranges_pooled': len(pool), 'reports_to_pilot': reported[0],
        }

    load_job_env(args.sandbox)            # the report key rides in the sandbox's environment file
    CallHome(default_flag(args.sandbox), status_key(), status).start()

    def record():
        # The record shipped off the node as it is made (es_record.py): what
        # a preempted job leaves behind is the last of these.
        running = [c for c in list(closes)]
        return {
            'kind': 'es_record', 'version': 1, 'stamp': args.stamp,
            'payload_version': payload_version,
            'pandaid': os.environ.get('PANDAID', ''), 'slots': args.slots,
            'started_at': started, 'deadline_s': args.deadline_s, 'margin_s': args.margin_s,
            'taking': taking,
            'done': [trim_unit(r) for r in list(summary['done'])],
            'failed': [trim_unit(r) for r in list(summary['failed'])],
            'closes': [{k: c.get(k) for k in ('index', 'ok', 'outcome', 'events', 'started_at',
                                               'ended_at', 'wall_s', 'did')}
                       for c in list(summary['closes'])],
            'closing': [{'index': c.index, 'started_at': c.started,
                         'units': [u[1] for u in c.units]} for c in running],
            'awaiting_close': [trim_unit(dict(u[2], slot=u[0].index))
                               for u in list(pending) + [u for c in running for u in c.units]],
            'in_flight': [{'unit_id': s.unit, 'slot': s.index, 'started_at': s.taken_at,
                           'events': s.unit_events,
                           'range': {'startEvent': s.unit_range.get('startEvent'),
                                     'lastEvent': s.unit_range.get('lastEvent')}}
                          for s in list(slots) if s.unit is not None],
        }

    try:
        with open(os.path.join(args.payload, 'VERSION')) as f:
            payload_version = f.readline().strip()
    except OSError:
        payload_version = ''
    shipped = RecordOut(record).start()

    def report(slot, uid, record, ok, range_ids, extra=''):
        wall = record.get('wall_s') or 0
        for rid in range_ids:
            if ok:
                feed.report(f"{os.path.join(slot.outbox, uid, 'unit.json')},ID:{rid},CPU:{wall},WALL:{wall}")
            else:
                feed.report(f"ERR_ATHENAMP_PROCESS {rid}: {extra or record.get('message', 'failed')}")
            reported[0] += 1

    while True:
        # Results first: a finished unit frees its slot; it is reported now,
        # or held for its close when it handed its RECO over.
        for s in slots:
            r = s.result()
            if r is None:
                continue
            uid, record, ok = r
            record.setdefault('wall_s', round(time.time() - (s.taken_at or time.time()), 1))
            record['slot'] = s.index                    # which slot ran it: the slot occupancy plot
            record.setdefault('started_at', s.taken_at)
            range_ids = list(s.range_ids)
            if ok:
                unit_walls.append((int(record.get('events') or 0), float(record['wall_s'])))
                shipped.nudge()
            if ok and record.get('handoff'):
                pending.append((s, uid, record, range_ids))
                log(f"unit {uid} ({len(range_ids)} ranges): done in {record['wall_s']} s, "
                    f"{record['handoff'].get('events')} events handed over; {len(pending)} awaiting a close")
                continue
            report(s, uid, record, ok, range_ids)
            (summary['done'] if ok else summary['failed']).append(record)
            log(f"unit {uid} ({len(range_ids)} ranges): {'done' if ok else 'failed'} in {record['wall_s']} s, dids {record.get('dids')}")
        # Closes that ended: their units' ranges are reported by the outcome.
        for c in list(closes):
            rec = c.result()
            if rec is None:
                continue
            closes.remove(c)
            summary['closes'].append(rec)
            for slot, uid, record, range_ids in c.units:
                record['close'] = rec['index']
                if rec['ok']:
                    record['dids'] = [rec.get('did')]
                    report(slot, uid, record, True, range_ids)
                    summary['done'].append(record)
                else:
                    report(slot, uid, record, False, range_ids, extra=f"close {rec['index']} failed: {rec.get('message')}")
                    summary['failed'].append(record)
            shipped.nudge()
            log(f"close {rec['index']}: {rec.get('outcome')} {rec.get('did')} "
                f"({rec.get('events')} events, {len(c.units)} units) in {rec['wall_s']} s"
                + ('' if rec['ok'] else f"; {rec.get('message')}"))
        if args.preempt_at_s and time.time() - started >= args.preempt_at_s:
            # TEST AND DEMO ONLY: the node is taken away. What is lost is the
            # processing cut off in the slots, and the units finished since
            # the last close that stood, whose output never left the node
            # (with a running close's, which dies with it).
            cut = time.time()
            feed.exhausted = True                       # nothing more is asked of the pilot
            shipped.halt()                              # the node is gone: nothing more leaves it
            for s in slots:
                if s.unit is not None:
                    summary['interrupted'].append({
                        'unit_id': s.unit, 'slot': s.index, 'status': 'interrupted',
                        'started_at': s.taken_at, 'ended_at': cut,
                        'wall_s': round(cut - s.taken_at, 1), 'events': s.unit_events,
                        'range': s.unit_range})
                s.kill()
            for c in closes:
                kill_group(c.proc)
                pending = c.units + pending
            for _, _, record, _ in pending:
                record['status'] = 'unshipped'
                summary['unshipped'].append(record)
            summary['preempted'] = {
                'at': cut, 'after_s': round(cut - started, 1),
                'interrupted_units': len(summary['interrupted']),
                'interrupted_events': sum(u['events'] for u in summary['interrupted']),
                'unshipped_units': len(summary['unshipped']),
                'unshipped_events': sum(int(r.get('events') or 0) for r in summary['unshipped'])}
            log(f"PREEMPTED at {cut - started:.0f} s: {summary['preempted']}")
            break
        past_deadline = args.deadline_s and time.time() - started > args.deadline_s - args.margin_s
        if past_deadline and taking:
            # The ranges never taken, on the pilot's side or pooled here,
            # go back to the server unreported when the job ends.
            log(f"deadline margin reached: taking no further range ({len(pool)} pooled)")
            taking = False
            feed.exhausted = True
            pool.clear()
            summary['untaken_at_deadline'] = True
        if taking and not slots and pool.first():
            # Every slot starts at once, as soon as the first range names the
            # input file: their start (container, geometry, resident
            # EICrecon) overlaps the gathering of their first units, which
            # wait in their inboxes. Started one by one as each unit became
            # ready, the starts ran in series behind the ranges (Perlmutter
            # job 3618884: slot 63 began 9 minutes after slot 0).
            # The pilot's staged copy of the range's LFN; without one
            # (direct-access queues, npps0 outside the SCDF perimeter) the
            # payload streams from the door per range, as production does.
            if input_path is None:
                input_path = find_input(os.path.dirname(args.sandbox), pool.first().get('LFN', '')) or ''
            log(f"input: {input_path or 'not staged; the payload reads the door per range'}")
            slots = [Slot(i, args, input_path) for i in range(args.slots)]
            log(f"{len(slots)} slots started")
        if taking and slots:
            # Feed every free slot what the pool has now.
            # Near the deadline a slot's last unit is cut to what fits before
            # the margin, so every slot ends together there rather than
            # running a whole unit into it (job 3618896: slots ended over ten
            # minutes of the margin); a slot with too little time left for a
            # unit takes none.
            cap = 0
            rate = seconds_per_event(unit_walls) if args.deadline_s else None
            if rate:
                fit = int((started + args.deadline_s - args.margin_s - time.time()) / rate)
                if fit < max(1, args.min_unit_events):
                    cap = -1
                elif args.events_per_unit and fit < args.events_per_unit:
                    cap = fit
            fed = False
            for slot in [s for s in slots if s.free()] if cap >= 0 else []:
                members = pool.take_unit(args.events_per_unit, min_events=args.min_unit_events,
                                         max_events=cap)
                if members and not template:
                    template.update(members[0])
                if not members and args.loop and pool.complete and not len(pool) and template:
                    members = replay_unit(template, replay, cap or args.events_per_unit or 1, args.expected_events)
                if not members:
                    pool.want()                 # a slot waits on the pool
                    break
                slot.give(unit_spec(members, args.events_per_unit, file_path, ext, args.stamp))
                fed = True
            if fed:
                continue
        elif taking:
            pool.want()                         # nothing pooled yet
        in_flight = [s for s in slots if s.unit is not None]
        # A close: on the cadence, or the last one when nothing else is
        # coming; one at a time.
        drained = not taking or (pool.exhausted and not len(pool) and not args.loop)
        if pending and not closes and (time.time() - last_close >= args.close_s
                                       or (drained and not in_flight)):
            closes.append(Close(len(summary['closes']) + len(closes) + 1, args, pending))
            pending, last_close = [], time.time()
        if drained and not in_flight and not pending and not closes:
            break
        time.sleep(2)
    # The pilot takes one report per pass of its loop, ten milliseconds
    # each, and drops what it has not taken when the payload exits (job
    # 3556537: 116 of a close's 326 reports landed); a burst of reports
    # is given its time before the harness ends.
    grace = min(300.0, 5.0 + 0.05 * reported[0])
    log(f"no more ranges: {len(summary['done'])} done, {len(summary['failed'])} failed; "
        f"{grace:.0f} s for the pilot to take the last {reported[0]} reports, then stopping slots")
    time.sleep(grace)
    for s in slots:
        s.stop()
    for s in slots:
        s.wait(120)
    summary['wall_s'] = round(time.time() - started, 1)
    shipped.stop()
    summary['ended_at'] = time.time()
    with open(args.summary, 'w') as f:
        json.dump(summary, f)
    return 0


if __name__ == '__main__':
    sys.exit(main())
