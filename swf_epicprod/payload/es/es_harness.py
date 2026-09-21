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
range after; K is the loss quantum, the block's index names the chunk. The input file is the pilot's staged copy of the range's
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
import time


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
                 + "".join(f" --env {kv}" for kv in (args.env or [])))
        cmd = [runtime, 'exec', '--cleanenv', '-B', args.sandbox, '-B', args.work]
        if input_path:
            cmd += ['-B', os.path.dirname(input_path)]
        if os.path.isdir('/cvmfs'):
            cmd += ['-B', '/cvmfs']
        cmd += ['--pwd', args.sandbox, args.image, '/bin/bash', '-c', inner]
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


def drain(feed, pool):
    """Every range the feed has into the pool, kept in event order: the
    server hands a job's ranges in no particular order (job 3556341 got
    6 8 10 3 15 4 ...), and a unit needs consecutive events."""
    while not feed.exhausted:
        rng = feed.ask()
        if rng is None:
            break
        pool.append(rng)
    pool.sort(key=lambda r: (r.get('LFN', ''), int(r['startEvent'])))


def take_unit(pool, per_unit):
    """The ranges of the next unit off the pool: one range, or up to
    per_unit consecutive ranges within one block of per_unit events
    (block b is events b*K+1 to (b+1)*K), so that a unit is a chunk of
    the row and its block index names its outputs. Empty when the pool
    is."""
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
                          'done': [], 'failed': [], 'untaken_at_deadline': False}
    input_path = args.input
    pool = []
    log(f"harness up: {args.slots} slots, image {args.image}, row {row[:2]}, "
        f"unit {args.events_per_unit or 'one range'} events, "
        f"deadline {args.deadline_s or 'none'} s, margin {args.margin_s} s")
    while True:
        # Results first: a finished unit frees its slot and is reported.
        for s in slots:
            r = s.result()
            if r is None:
                continue
            uid, record, ok = r
            wall = record.get('wall_s') or round(time.time() - (s.taken_at or time.time()), 1)
            # Every range of the unit is reported, with the unit's receipt.
            for rid in s.range_ids:
                if ok:
                    receipt = os.path.join(s.outbox, uid, 'unit.json')
                    feed.report(f"{receipt},ID:{rid},CPU:{wall},WALL:{wall}")
                else:
                    feed.report(f"ERR_ATHENAMP_PROCESS {rid}: {record.get('message', 'failed')}")
            (summary['done'] if ok else summary['failed']).append(record)
            log(f"unit {uid} ({len(s.range_ids)} ranges): {'done' if ok else 'failed'} in {wall} s, dids {record.get('dids')}")
        past_deadline = args.deadline_s and time.time() - started > args.deadline_s - args.margin_s
        if past_deadline and (pool or not feed.exhausted):
            # The ranges never taken, on the pilot's side or pooled here,
            # go back to the server unreported when the job ends.
            log(f"deadline margin reached: taking no further range ({len(pool)} pooled)")
            feed.exhausted = True
            pool = []
            summary['untaken_at_deadline'] = True
        drain(feed, pool)
        if pool:
            # Feed a free slot, starting slots lazily so the first range's
            # input names the file before any container starts.
            free = [s for s in slots if s.free()]
            if free or len(slots) < args.slots:
                members = take_unit(pool, args.events_per_unit)
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
        if feed.exhausted and not pool and not in_flight:
            break
        time.sleep(2)
    log(f"no more ranges: {len(summary['done'])} done, {len(summary['failed'])} failed; stopping slots")
    for s in slots:
        s.stop()
    for s in slots:
        s.wait(120)
    summary['wall_s'] = round(time.time() - started, 1)
    with open(args.summary, 'w') as f:
        json.dump(summary, f)
    return 0


if __name__ == '__main__':
    sys.exit(main())
