#!/usr/bin/env python3
"""The Event Service harness's record, shipped off the node as it is made
(docs/JOB_REPORTING.md, Event Service record; NODE_EVENT_DISPATCHER.md).

A job that ends normally hands its record to the pilot, which puts it in
the job's metadata. A preempted job ends with the node: no final report,
and the record dies with the worker unless it has already left. So the
harness writes it, like its output, as it goes: one object,
``reports/<PanDA job id>/es.json``, overwritten at each unit that
finishes, each close, and at least every minute. The last write is the
record at the cut, and its time bounds the cut to within the interval.

The same store, write key and signed PUT as the payload report and call
home (``report_out.py``); every failure is a line on stderr and never
costs the job; a hard cap on writes per job, a constant, bounds a defect
that writes in a loop.
"""
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from report_out import put_object  # noqa: E402

INTERVAL_S = 60
# Writes are at least SPACING_S apart, so a unit finishing every few seconds
# on 128 slots coalesces into one write a minute: about 240 for a four-hour
# job (Perlmutter task 41262 wrote once every 5 s and reached the cap at
# about 2 h 10 min, its record then frozen). A minute is well inside the ES
# close-out's quiet limit and the dashboard's staleness limit. The cap is not
# raisable by configuration.
SPACING_S = 60
MAX_WRITES = 1500
UNIT_FIELDS = ('unit_id', 'slot', 'status', 'started_at', 'ended_at', 'wall_s',
               'simulation', 'reconstruction', 'phases', 'timing_error',
               'events', 'events_reconstructed', 'close', 'message', 'unit_path')


def trim_unit(record):
    """A unit record cut to what the job page draws and counts."""
    out = {k: record.get(k) for k in UNIT_FIELDS if record.get(k) is not None}
    rng = record.get('range') or {}
    if rng:
        out['range'] = {'startEvent': rng.get('startEvent'), 'lastEvent': rng.get('lastEvent')}
    return out


def record_key():
    pandaid = os.environ.get('PANDAID', '').strip()
    return f'reports/{pandaid}/es.json' if pandaid.isdigit() else None


def configured():
    return bool(record_key()) and all(os.environ.get(k, '').strip() for k in (
        'REPORT_OUT_BUCKET', 'REPORT_OUT_ACCESS_KEY_ID', 'REPORT_OUT_SECRET_ACCESS_KEY'))


# Each close's units leave the node with the close (``close_key``): the
# bookkeeping of a job ships as it is made, a close at a time, and is not
# carried whole by every write of the job's record nor exported at its end.
MAX_CLOSE_WRITES = 4000


def close_key(index):
    pandaid = os.environ.get('PANDAID', '').strip()
    return f'reports/{pandaid}/close-{int(index):04d}.json' if pandaid.isdigit() else None


def ship_close(close, units):
    """Ship one close and its units' records, ``reports/<job>/close-NNNN.json``,
    on a thread of its own so the harness never waits on the store. Returns
    the thread, or None when the store is not configured."""
    if not configured() or ship_close.sent >= MAX_CLOSE_WRITES:
        return None
    ship_close.sent += 1
    body = json.dumps({'kind': 'es_close', 'version': 1, 'pandaid': os.environ.get('PANDAID'),
                       'close': close, 'units': [trim_unit(u) for u in units],
                       'written_at': time.time()}).encode('utf-8')
    key = close_key(close.get('index'))

    def put():
        try:
            ok, detail = put_object(
                body, os.environ['REPORT_OUT_BUCKET'].strip(), key,
                os.environ.get('REPORT_OUT_REGION', 'us-east-1'),
                os.environ['REPORT_OUT_ACCESS_KEY_ID'].strip(),
                os.environ['REPORT_OUT_SECRET_ACCESS_KEY'].strip(),
                endpoint=os.environ.get('REPORT_OUT_ENDPOINT', ''),
                session_token=os.environ.get('REPORT_OUT_SESSION_TOKEN', ''))
        except Exception as e:                                   # noqa: BLE001
            ok, detail = False, str(e)
        if not ok:
            print(f'[es_record] {key} failed: {detail}', file=sys.stderr, flush=True)

    thread = threading.Thread(target=put, name=f'es-close-{close.get("index")}', daemon=True)
    thread.start()
    return thread


ship_close.sent = 0


class RecordOut:
    """Writes snapshot() to the store every INTERVAL_S and whenever nudged."""

    def __init__(self, snapshot, interval=INTERVAL_S):
        self.snapshot, self.interval = snapshot, interval
        self.key = record_key()
        self.sent = 0
        self.wake = threading.Event()
        self.urgent = threading.Event()     # a flush or a stop: no spacing
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self._run, name='es-record', daemon=True)

    def start(self):
        if not configured():
            print('[es_record] no store or job id in the environment; the record is not shipped',
                  file=sys.stderr, flush=True)
            return self
        self.thread.start()
        return self

    def nudge(self):
        self.wake.set()

    def flush(self):
        """Write now, outside the spacing: a close that stood. Its units are
        credited from the record when the node is lost (es_closeout.py), and
        a close missing from it is in Rucio but simulated again by a later
        job; about one close every two minutes, inside the write cap."""
        self.urgent.set()
        self.wake.set()

    def write(self):
        if self.sent >= MAX_WRITES:
            return False
        try:
            body = dict(self.snapshot(), written_at=time.time(), sequence=self.sent)
            ok, detail = put_object(
                json.dumps(body).encode('utf-8'),
                os.environ['REPORT_OUT_BUCKET'].strip(), self.key,
                os.environ.get('REPORT_OUT_REGION', 'us-east-1'),
                os.environ['REPORT_OUT_ACCESS_KEY_ID'].strip(),
                os.environ['REPORT_OUT_SECRET_ACCESS_KEY'].strip(),
                endpoint=os.environ.get('REPORT_OUT_ENDPOINT', ''),
                session_token=os.environ.get('REPORT_OUT_SESSION_TOKEN', ''))
        except Exception as e:                                   # noqa: BLE001
            ok, detail = False, str(e)
        self.sent += 1
        if not ok:
            print(f'[es_record] write {self.sent} failed: {detail}', file=sys.stderr, flush=True)
        return ok

    def halt(self):
        """Stop without another write (a simulated preemption: the node,
        and the channel with it, is gone)."""
        self.sent = MAX_WRITES
        self.stopping.set()
        self.urgent.set()
        self.wake.set()

    def stop(self):
        """One last write with the final state, then stop."""
        if self.thread.is_alive():
            self.stopping.set()
            self.urgent.set()
            self.wake.set()
            self.thread.join(timeout=30)

    def _run(self):
        while True:
            nudged = self.wake.wait(self.interval)
            if nudged and not self.stopping.is_set():
                # Nudges inside the spacing join this write; a flush or a
                # stop does not wait.
                self.urgent.wait(SPACING_S)
            self.wake.clear()
            self.urgent.clear()
            self.write()
            if self.stopping.is_set():
                return
