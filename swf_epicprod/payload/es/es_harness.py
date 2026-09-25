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
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from call_home import CallHome, default_flag, load_job_env, status_key  # noqa: E402


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
            time.sleep(0.05)
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
                                     stderr=subprocess.STDOUT)
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
        self.proc = subprocess.Popen(cmd, stdout=self.log, stderr=subprocess.STDOUT)
        log(f"slot {index} started (pid {self.proc.pid})")

    def free(self):
        return self.unit is None and self.proc.poll() is None

    def give(self, spec):
        uid = spec['unit_id']
        self.range_ids = [r['eventRangeID'] for r in spec['ranges']]
        tmp = os.path.join(self.inbox, f'.{uid}.tmp')
        with open(tmp, 'w') as f:
            json.dump(spec, f)
        os.rename(tmp, os.path.join(self.inbox, f'{uid}.unit.json'))
        self.unit, self.taken_at = uid, time.time()

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

    def __init__(self, feed):
        self.feed = feed
        self.ranges = []
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self._gather, name='pool', daemon=True)
        self.thread.start()

    def _gather(self):
        while not self.feed.exhausted:
            rng = self.feed.ask()
            if rng is None:
                break
            with self.lock:
                self.ranges.append(rng)
                self.ranges.sort(key=lambda r: (r.get('LFN', ''), int(r['startEvent'])))

    @property
    def exhausted(self):
        return self.feed.exhausted and not self.thread.is_alive()

    def __len__(self):
        with self.lock:
            return len(self.ranges)

    def clear(self):
        with self.lock:
            self.ranges = []

    def take_unit(self, per_unit):
        """The ranges of the next unit: one range, or up to per_unit
        consecutive ranges within one block of per_unit events (block b
        is events b*K+1 to (b+1)*K), so that a unit is a chunk of the row
        and its block index names its outputs. A block is cut when it is
        whole, or when no more ranges are coming; empty otherwise."""
        with self.lock:
            if not self.ranges:
                return []
            if per_unit < 1:
                return [self.ranges.pop(0)]
            first = self.ranges[0]
            block = (int(first['startEvent']) - 1) // per_unit
            members = [first]
            for rng in self.ranges[1:]:
                prev = members[-1]
                start = int(rng['startEvent'])
                if (rng.get('LFN') != first.get('LFN') or start != int(prev['lastEvent']) + 1
                        or (start - 1) // per_unit != block or len(members) >= per_unit):
                    break
                members.append(rng)
            # Whole is every event of the block: a block whose first events
            # are still coming is not whole for reaching its last (job
            # 3556539 cut events 2-5 and then 1 alone).
            if len(members) < per_unit and not self.exhausted:
                return []                       # its block is still arriving
            del self.ranges[:len(members)]
            return members


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
                          'started_at': started}
    input_path = args.input
    pool = Pool(feed)
    pending, closes, last_close = [], [], time.time()     # units handed over, awaiting a close
    taking = True                                          # False past the deadline's margin
    log(f"harness up: {args.slots} slots, image {args.image}, row {row[:2]}, "
        f"unit {args.events_per_unit or 'one range'} events, "
        f"close every {args.close_s or 'unit'} s, "
        f"deadline {args.deadline_s or 'none'} s, margin {args.margin_s} s")

    reported = [0]                                          # reports sent since the harness last waited on the pilot

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
            log(f"close {rec['index']}: {rec.get('outcome')} {rec.get('did')} "
                f"({rec.get('events')} events, {len(c.units)} units) in {rec['wall_s']} s"
                + ('' if rec['ok'] else f"; {rec.get('message')}"))
        past_deadline = args.deadline_s and time.time() - started > args.deadline_s - args.margin_s
        if past_deadline and taking:
            # The ranges never taken, on the pilot's side or pooled here,
            # go back to the server unreported when the job ends.
            log(f"deadline margin reached: taking no further range ({len(pool)} pooled)")
            taking = False
            feed.exhausted = True
            pool.clear()
            summary['untaken_at_deadline'] = True
        if taking and len(pool):
            # Feed a free slot, starting slots lazily so the first range's
            # input names the file before any container starts.
            free = [s for s in slots if s.free()]
            if free or len(slots) < args.slots:
                members = pool.take_unit(args.events_per_unit)
                if members:
                    rng = members[0]
                    if input_path is None:
                        # The pilot's staged copy of the range's LFN; without
                        # one (direct-access queues, npps0 outside the SCDF
                        # perimeter) the payload streams from the door per
                        # range, as production does.
                        input_path = find_input(os.path.dirname(args.sandbox), rng.get('LFN', '')) or ''
                        log(f"input: {input_path or 'not staged; the payload reads the door per range'}")
                    if not free:
                        slots.append(Slot(len(slots), args, input_path))
                        free = [slots[-1]]
                    free[0].give(unit_spec(members, args.events_per_unit, file_path, ext, args.stamp))
                    continue
        in_flight = [s for s in slots if s.unit is not None]
        # A close: on the cadence, or the last one when nothing else is
        # coming; one at a time.
        drained = not taking or (pool.exhausted and not len(pool))
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
    summary['ended_at'] = time.time()
    with open(args.summary, 'w') as f:
        json.dump(summary, f)
    return 0


if __name__ == '__main__':
    sys.exit(main())
