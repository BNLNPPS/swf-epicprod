#!/usr/bin/env python3
"""Call home: the job's current status, written to object storage only
while the job is in PanDA debug mode (swf-epicprod docs/JOB_REPORTING.md).

The pilot keeps a file in the job work directory while the server has
the job in debug mode (pilot3 PR 224, config.Pilot.debug_mode_file,
pilot_debug_mode.json). This checks for that file every POLL_S seconds;
while it exists it writes the job's status as one object,
status/<PanDA job id>.json, overwritten, at once when the file appears
and every EPICPROD_CALL_HOME_S seconds (default 600) after. With the
file absent it writes nothing, so debug mode, set per job on the server,
is the switch, and it reaches running jobs within a minute in both
directions.

Standard library only, like report_out.py, whose signed PUT it uses.
Nothing here may cost a job: every failure is a line on stderr, and the
next interval tries again.

Two uses:
- in process (the Event Service harness): CallHome(flag, key, snapshot).start(),
  snapshot a callable returning the status dict;
- as a background program beside run.sh:
    call_home.py --flag <path> --key <key> --file payload-report.json --watch-pid <pid>
  which sends the file's current content and exits when <pid> ends.
"""

import argparse
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from report_out import put_object  # noqa: E402

DEBUG_MODE_FILE = 'pilot_debug_mode.json'
POLL_S = 60
INTERVAL_S = float(os.environ.get('EPICPROD_CALL_HOME_S', '600'))
# A hard cap per job, not raisable by configuration: a defect that writes
# in a loop is the one unbounded cost of this channel. 150 writes is 25
# hours at the default interval.
MAX_WRITES = 150


def log(msg):
    print(f"[call-home {time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def default_flag(workdir):
    """The pilot's debug-mode file for a payload running in workdir.

    The pilot writes it into the job directory (PanDA_Pilot-<id>/), and the
    payload runs in that directory or in the workDir/ the sandbox unpacks
    into beneath it, so both are checked: the file is the first of the two
    that exists, else the job directory's path.
    """
    if os.environ.get('EPICPROD_DEBUG_FLAG'):
        return os.environ['EPICPROD_DEBUG_FLAG']
    here = os.path.abspath(workdir)
    for d in (here, os.path.dirname(here)):
        if os.path.exists(os.path.join(d, DEBUG_MODE_FILE)):
            return os.path.join(d, DEBUG_MODE_FILE)
    parent = os.path.dirname(here)
    job_dir = parent if os.path.basename(here) == 'workDir' else here
    return os.path.join(job_dir, DEBUG_MODE_FILE)


def load_job_env(sandbox):
    """REPORT_OUT_* and PANDAID from the sandbox's environment-*.sh (the file
    run.sh sources), for a caller that does not run under it, such as the
    Event Service harness. Values already in the environment win."""
    import glob
    for path in sorted(glob.glob(os.path.join(sandbox, 'environment-*.sh'))):
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip().removeprefix('export ').strip()
                    key, sep, value = line.partition('=')
                    key = key.strip()
                    if sep and (key.startswith('REPORT_OUT_') or key == 'PANDAID') and not os.environ.get(key):
                        os.environ[key] = value.strip().strip('"').strip("'")
        except OSError as e:
            log(f"cannot read {path}: {e}")


def status_key():
    """status/<PanDA job id>.json; None when the job has no id to report under."""
    pandaid = os.environ.get('PANDAID', '').strip()
    return f'status/{pandaid}.json' if pandaid.isdigit() else None


def configured():
    """True when the job environment carries the destination and the credential."""
    return all(os.environ.get(k, '').strip() for k in (
        'REPORT_OUT_BUCKET', 'REPORT_OUT_ACCESS_KEY_ID', 'REPORT_OUT_SECRET_ACCESS_KEY'))


def send(body, key):
    """Write one status object. Returns True when written."""
    ok, detail = put_object(
        body, os.environ['REPORT_OUT_BUCKET'].strip(), key,
        os.environ.get('REPORT_OUT_REGION', 'us-east-1'),
        os.environ['REPORT_OUT_ACCESS_KEY_ID'].strip(),
        os.environ['REPORT_OUT_SECRET_ACCESS_KEY'].strip(),
        endpoint=os.environ.get('REPORT_OUT_ENDPOINT', ''),
        session_token=os.environ.get('REPORT_OUT_SESSION_TOKEN', ''))
    if not ok:
        log(f"status not sent: {detail}")
    return ok


class CallHome:
    """A daemon thread that sends snapshot() while the debug-mode file exists."""

    def __init__(self, flag, key, snapshot, interval=INTERVAL_S, poll=POLL_S, clock=time.time):
        self.flag, self.key, self.snapshot = flag, key, snapshot
        self.interval, self.poll, self.clock = interval, poll, clock
        self.sent, self.last = 0, None
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self._run, name='call-home', daemon=True)

    def start(self):
        if not self.key or not configured():
            log("off: no job id or no report credential in the job environment")
            return self
        log(f"watching {self.flag}; status to {self.key} every {self.interval:.0f} s while it exists")
        self.thread.start()
        return self

    def stop(self):
        self.stopping.set()

    def tick(self):
        """One check: send when the file exists and the interval has passed.
        Returns True when a status was sent."""
        if self.sent >= MAX_WRITES or not os.path.exists(self.flag):
            if self.last is not None and not os.path.exists(self.flag):
                log("debug mode off: status paused")
                self.last = None
            return False
        now = self.clock()
        if self.last is not None and now - self.last < self.interval:
            return False
        self.last = now                     # a failed send waits its interval too
        try:
            status = dict(self.snapshot() or {})
        except Exception as e:              # noqa: BLE001 - a status never fails a job
            status = {'snapshot_error': f'{type(e).__name__}: {e}'}
        status.update(sent_at=int(now), sequence=self.sent, pandaid=os.environ.get('PANDAID', ''),
                      interval_s=self.interval)
        if send(json.dumps(status, default=str).encode('utf-8'), self.key):
            self.sent += 1
            if self.sent == MAX_WRITES:
                log(f"cap of {MAX_WRITES} writes reached; no further status from this job")
            return True
        return False

    def _run(self):
        while not self.stopping.is_set():
            self.tick()
            self.stopping.wait(self.poll)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--flag', default=None, help='the pilot debug-mode file (default: in the current directory)')
    ap.add_argument('--key', default=None, help='object key (default: status/<PANDAID>.json)')
    ap.add_argument('--file', required=True, help='the status document to send (read at each send)')
    ap.add_argument('--watch-pid', type=int, required=True, help='exit when this process ends')
    args = ap.parse_args()

    def snapshot():
        with open(args.file) as f:
            return {'report': json.load(f)}

    home = CallHome(args.flag or default_flag(os.getcwd()), args.key or status_key(), snapshot)
    if not home.key or not configured():
        home.start()                        # logs why it is off
        return 0
    log(f"watching {home.flag}; status to {home.key} every {home.interval:.0f} s while it exists")
    checked = 0.0
    while True:
        # The watched process is checked every few seconds, so this never
        # outlives the payload by more than that.
        try:
            os.kill(args.watch_pid, 0)
        except OSError:
            return 0
        if time.time() - checked >= POLL_S:
            checked = time.time()
            home.tick()
        time.sleep(5)


if __name__ == '__main__':
    sys.exit(main())
