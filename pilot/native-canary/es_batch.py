"""Opt-in, bounded ES IPC batches; PanDA still receives its normal bulk protocol."""
import collections
import json
import logging
import os
import re
import tarfile
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

RESULT_PREFIX = 'EPICPROD_ES_RESULT_V1 '
MAX_MESSAGE_RANGES = 512
# Ranges per core in one server acquisition, at most one every two seconds
# (throttle_acquire). One per core held Perlmutter job 6071469 (256 cores)
# to about 100 ranges a second, a fifth of what 128 slots of 0.26 s gun
# events simulate; eight per core feed about 1,000 a second.
ACQUIRE_PER_CORE = 8
API_BATCH_RANGES = 10000


def enabled():
    return os.environ.get('EPICPROD_SWITCH_ES_BATCH_CHANNEL') == '1'


def acquire_count(corecount):
    """The ranges one server acquisition asks for."""
    cores = max(1, int(corecount or 1))
    return ACQUIRE_PER_CORE * cores if enabled() else cores


def request_count(message):
    if not enabled() or message == 'Ready for events':
        return 1
    match = re.fullmatch(r'Ready for events: ([0-9]+)', message)
    if not match or not 1 <= int(match[1]) <= MAX_MESSAGE_RANGES:
        raise ValueError('Invalid bounded ES request')
    return int(match[1])


def decode_result(message, expected_pandaid):
    if not enabled():
        raise ValueError('ES batch channel was not enabled')
    body = json.loads(message[len(RESULT_PREFIX):])
    ids = body.get('ids')
    if not isinstance(ids, list) or not 1 <= len(ids) <= MAX_MESSAGE_RANGES:
        raise ValueError('Invalid ES result batch size')
    parts = []
    for rid in ids:
        if not isinstance(rid, str) or not re.fullmatch(r'\d+-\d+-\d+-\d+-\d+', rid):
            raise ValueError('Invalid ES range identity')
        p = tuple(map(int, rid.split('-')))
        if p[1] != int(expected_pandaid):
            raise ValueError('ES result belongs to another consumer')
        parts.append(p)
    # One unit is a run of the job's ranges in one file and attempt. Their ids
    # step by the range size: one for one-event ranges, ten for ten-event ranges
    # (nEventsPerFineGrainedRange). A step of one refused every unit of several
    # ten-event ranges; task 41431 credited 161 of its 738 units.
    step = parts[1][3] - parts[0][3] if len(parts) > 1 else 1
    if len(set(ids)) != len(ids) or step < 1 or any(
            p[:3] + p[4:] != parts[0][:3] + parts[0][4:] or
            (i and p[3] != parts[i - 1][3] + step) for i, p in enumerate(parts)):
        raise ValueError('ES result must be one contiguous owned unit')
    output = body.get('output')
    if not isinstance(output, str) or not output.startswith('/'):
        raise ValueError('ES result must name its absolute unit record')
    # The unit's events, from payload 0.25.31; one a range before it.
    events = body.get('events')
    if not isinstance(events, int) or isinstance(events, bool) or events < len(ids):
        events = len(ids)
    return {'id': ids[0], 'ids': ids, 'output': output, 'status': 'finished',
            'events': events, 'cpu': body.get('wall', 0), 'wall': body.get('wall', 0)}


# A failed unit, one message (EPICPROD_SWITCH_ES_FAILED_UNIT, which this pilot
# sets for its payload): before it the harness sent one ERR message a range,
# and the pilot reported each with a server call of its own, a second or more,
# inside the loop that answers the payload's range requests. In job 6254767
# (2026-10-27) a slot whose resident simulation had died failed 961 units of
# 500 one-event ranges in no time each; the job's range requests waited
# behind their 480,000 messages, days of such calls, with every slot idle.
FAILED_PREFIX = 'EPICPROD_ES_FAILED_V1 '


def advertise():
    """Before the payload starts: tell it this pilot takes a failed unit as
    one message. The payload inherits the pilot's environment."""
    if enabled():
        os.environ['EPICPROD_SWITCH_ES_FAILED_UNIT'] = '1'


def decode_failed(message, expected_pandaid):
    """A failed unit's message as the executor's failed message. Never
    raises: the ES process stops on a message it cannot handle, so a
    malformed report is logged and its ranges left to the server, which
    releases them with the job's other unfinished ranges."""
    try:
        body = json.loads(message[len(FAILED_PREFIX):])
        ids = [rid for rid in body.get('ids') or []
               if isinstance(rid, str) and re.fullmatch(r'\d+-\d+-\d+-\d+-\d+', rid)
               and int(rid.split('-')[1]) == int(expected_pandaid)]
        text = str(body.get('message') or 'failed')[:300]
    except (TypeError, ValueError, AttributeError) as exc:
        logger.error(f'unreadable failed-unit message, its ranges left to the server: {exc}; {message[:300]}')
        ids, text = [], 'unreadable'
    return {'id': ids[0] if ids else 'none', 'ids': ids[:MAX_MESSAGE_RANGES], 'status': 'failed', 'message': text}


def queue_failed(executor, messages):
    """The executor's failed ranges, queued for its next pass (report_failed);
    never a server call here, in the loop that answers the payload."""
    pending = executor.__dict__.setdefault('_epic_failed', collections.deque())
    pending.extend(messages)


def report_failed(executor, final=False):
    """Send the queued failed ranges to the server in bulk, REPORT_RANGES a
    call, from the executor's pass. A call that fails puts them back for the
    next pass, 30 s on; at the job's end they are left, and the server releases
    them with the job's other unfinished ranges, as it does failed ones
    (job_complex_module.check_fine_grained_processing): a failed report is
    bookkeeping, and no event depends on it."""
    pending = getattr(executor, '_epic_failed', None)
    if not pending or (not final and time.time() < getattr(executor, '_epic_failed_retry_at', 0)):
        return
    from pilot.common.errorcodes import ErrorCodes
    messages = []
    while pending:
        messages.append(pending.popleft())
    ranges = [{'eventRangeID': rid, 'eventStatus': 'failed', 'errorCode': ErrorCodes.UNKNOWNPAYLOADFAILURE}
              for message in messages for rid in message.get('ids', [message['id']]) if rid != 'none']
    for first in range(0, len(ranges), REPORT_RANGES):
        batch = ranges[first:first + REPORT_RANGES]
        try:
            data = _server('update_event_ranges', {'event_ranges': json.dumps([{'eventRanges': batch}]), 'version': 1})
            if answers(data, len(batch)) is None:
                raise RuntimeError(f'update_event_ranges answered {str(data)[:300]}')
        except Exception as exc:                              # noqa: BLE001
            rest = [{'id': r['eventRangeID'], 'ids': [r['eventRangeID']]} for r in ranges[first:]]
            if final:
                logger.warning(f'{len(rest)} failed ranges unreported at the end: {exc}; '
                               'the server releases them with the job')
            else:
                logger.warning(f'report of {len(rest)} failed ranges failed: {exc}; again in 30 s')
                pending.extendleft(reversed(rest))
                executor._epic_failed_retry_at = time.time() + 30
            return
    if ranges:
        logger.info(f'reported {len(ranges)} failed ranges of {len(messages)} messages')


def range_events(messages):
    """The events of each range of ``messages``, in report order: a unit's
    events spread evenly over its ranges, one a range when it gives none."""
    out = []
    for message in messages:
        n = len(message.get('ids', [message['id']]))
        out.extend([(message.get('events') or n) / n] * n)
    return out


def answers(response, n):
    """The server's answers to ``n`` range updates: the first ``n`` of its
    Returns. What follows them answers no range and is ignored (until
    2026-10-27 the PostgreSQL job event-count statement appended a False);
    fewer than ``n`` is a call that failed."""
    returned = response.get('Returns') if isinstance(response, dict) else None
    if isinstance(returned, str):
        returned = json.loads(returned)
    if not isinstance(returned, list) or len(returned) < n:
        return None
    return returned[:n]


def stageout_gap(default=601):
    """Keep the established default; an explicit batch pilot can request >=5min."""
    raw = os.environ.get('EPICPROD_ES_STAGEOUT_GAP')
    if not raw:
        return default
    if not enabled() or not raw.isdigit() or not 300 <= int(raw) <= 3600:
        raise ValueError('ES stageout interval must be 300..3600 seconds on a batch pilot')
    return int(raw)


def pack_reports(pending):
    """One tar open; one stored record per unit, including legacy duplicates."""
    messages = []
    while pending:
        messages.append(pending.pop())
    if not messages:
        return [], None
    output = 'EventService_premerge_' + messages[0]['id'] + '.tar'
    kept, added = [], set()
    with tarfile.open(output, 'w') as archive:
        for message in messages:
            path = message['output']
            try:
                if path not in added:
                    archive.add(path, arcname=message['id'] + '/' + Path(path).name,
                                recursive=False)
                    added.add(path)
                kept.append(message)
            except OSError:
                retries = int(message.get('retries', 0))
                if retries < 3:
                    message['retries'] = retries + 1
                    pending.append(message)
    return kept, output


def update_finished(executor, messages, output, size, checksum, storage):
    """Flatten only at the normal server boundary, with bounded API requests."""
    zip_file = {'objstoreID': storage, 'lfn': os.path.basename(output), 'fsize': size, 'pathConvention': 1000}
    zip_file.update(checksum)
    send_finished(executor, messages, zip_file)


def send_finished(executor, messages, zip_file):
    ranges = [{'eventRangeID': rid, 'eventStatus': 'finished'}
              for message in messages for rid in message.get('ids', [message['id']])]
    weights = range_events(messages)
    for first in range(0, len(ranges), API_BATCH_RANGES):
        batch = ranges[first:first + API_BATCH_RANGES]
        chunk = {'eventRanges': batch}
        if zip_file is not None:
            chunk['zipFile'] = dict(zip_file, numEvents=int(round(sum(weights[first:first + API_BATCH_RANGES]))))
        response = executor.update_events({'version': 1, 'eventRanges': json.dumps([chunk])})
        returned = answers(response, len(batch))
        if returned is None or any(v is not True for v in returned):
            raise RuntimeError('PanDA did not acknowledge the entire owned event batch')
    executor.get_job().nevents += int(round(sum(weights)))


def throttle_acquire(process):
    """At most one normal bulk acquisition every two seconds on this candidate."""
    last = getattr(process, '_epic_last_acquire', None)
    if last is not None:
        time.sleep(max(0.0, last + 2.0 - time.monotonic()))
    process._epic_last_acquire = time.monotonic()


# The job's ranges held on the node from its start, and its finished ranges
# reported as each close stands. The pilot's communication manager is one
# thread, a pass a second, carrying range acquisitions, range reports and the
# job's updates in turn. A report of 10,000 ranges sent right behind another
# took the server 22 to 33 s (2,140 took 0.6 s), so each periodic stage-out's
# run of them held every acquisition for minutes, and the slots starved in
# those windows (Perlmutter job 6095121, 2026-10-26: about 215 events a second
# against the 440 its slots simulate). On a stop the executor also stops that
# manager before its forced stage-out, so a job's last reports could not
# reach the server at all. Both paths go to the server directly instead.
ACQUIRE_MAX = 10000         # ranges in one acquisition
REPORT_RANGES = 2000        # ranges in one report
FIRST_ACQUIRE = 256        # ranges in the first acquisition, before the range size is known
TAKE_GIVE_UP_S = 300        # an empty cache the server does not refill: answer "No more events"
# PanDA marks an acquisition's ranges sent to the job, committed, before it
# answers; an answer lost on the way (a server restart mid-answer, a broken
# connection) leaves them sent to the job and unknown to the pilot. PanDA's
# record of the job's ranges is the state: after a failed acquisition the
# prefetch reads it back and takes every range sent to this job that it never
# received. A PanDA restart at 12:17:32 ET on 2026-10-27 cut such an answer to
# job 6252252 (events 90,002-100,001), which then waited for the job's end
# and a job of its own. The read waits RECONCILE_DELAY_S after the failure, so
# a request the server was still finishing has committed.
RECONCILE_DELAY_S = 60


def _server(endpoint, body):
    """One call to the server's event API, as the communicator makes it."""
    from pilot.util import https
    from pilot.util.config import config
    url = os.environ.get('PANDA_SERVER_URL', config.Pilot.pandaserver)
    res = https.request2(f'{url}/api/v1/event/{endpoint}', json_body=body, panda=True)
    if not isinstance(res, dict) or not res.get('success'):
        raise RuntimeError(f'{endpoint} refused: {str(res)[:300]}')
    return res.get('data')


def _server_get(endpoint, params):
    """One GET of the server's event API."""
    from pilot.util import https
    from pilot.util.config import config
    url = os.environ.get('PANDA_SERVER_URL', config.Pilot.pandaserver)
    res = https.request2(f'{url}/api/v1/event/{endpoint}', params=params, panda=True, method='GET')
    if not isinstance(res, dict) or not res.get('success'):
        raise RuntimeError(f'{endpoint} refused: {str(res)[:300]}')
    return res.get('data')


def prefetch_size():
    """EPICPROD_ES_PREFETCH_EVENTS: the events a job holds on the node, in its
    ranges; unset is off. EPICPROD_ES_PREFETCH_RANGES, the name from before
    ranges of several events, is read the same way."""
    raw = (os.environ.get('EPICPROD_ES_PREFETCH_EVENTS')
           or os.environ.get('EPICPROD_ES_PREFETCH_RANGES', ''))
    if not raw or not enabled():
        return 0
    if not raw.isdigit() or not 1000 <= int(raw) <= 1000000:
        raise ValueError('EPICPROD_ES_PREFETCH_EVENTS must be 1000..1000000 on a batch pilot')
    return int(raw)


def range_span(rng):
    """The events of one range the server handed out (startEvent..lastEvent)."""
    try:
        return max(1, int(rng['lastEvent']) - int(rng['startEvent']) + 1)
    except (KeyError, TypeError, ValueError):
        return 1


class Prefetch:
    """The job's ranges, acquired from its start and refilled whenever they
    hold fewer than half of ``size`` events, ACQUIRE_MAX ranges a call, until
    the server has none left. The size is in events, so a node holds the same
    work whatever the range size: 200,000 ranges of ten events would have held
    two thirds of a 3M-event task. Ranges a job holds when it ends are released
    by the server's fine-grained accounting with every other range it did not
    finish. A failed acquisition is reconciled against PanDA's record of the
    job's ranges (_reconcile), so a lost answer loses nothing."""

    def __init__(self, job, size, stopped):
        self.ident = {'job_id': int(job.jobid), 'jobset_id': int(job.jobsetid), 'task_id': int(job.taskid)}
        self.size, self.stopped = size, stopped
        self.ranges = collections.deque()
        self.held = 0                   # events in self.ranges
        self.span = 1                   # events a range, from the last acquisition
        self.exhausted = False
        self.calls = self.failures = self.recovered = 0
        self.received = {}              # file id: bytearray, 1 at each process id received
        self.files = {}                 # file id: a received range's fields, the file's own
        self.lost_at = None             # when an acquisition failed and was not yet reconciled
        self.cond = threading.Condition()
        self.thread = threading.Thread(target=self._fill, name='es-prefetch', daemon=True)
        self.thread.start()

    def _note(self, ranges):
        """Record ranges as received, and each file's own fields."""
        for rng in ranges:
            parts = str(rng.get('eventRangeID', '')).split('-')
            if len(parts) != 5 or not parts[3].isdigit():
                continue
            seen, process = self.received.setdefault(parts[2], bytearray()), int(parts[3])
            if process >= len(seen):
                seen.extend(bytes(process + 1 - len(seen)))
            seen[process] = 1
            self.files.setdefault(parts[2], {k: v for k, v in rng.items()
                                             if k not in ('eventRangeID', 'startEvent', 'lastEvent')})

    def _was_received(self, parts):
        seen = self.received.get(parts[2])
        return seen is not None and int(parts[3]) < len(seen) and seen[int(parts[3])] == 1

    def _reconcile(self):
        """Take back the ranges PanDA has sent to this job that the pilot never
        received. Runs on the prefetch's own thread, so no acquisition is in
        flight while PanDA's record is read; the ranges of a fine-grained job
        are full ranges of the job's range size, and a range's process id is
        its first event (production_es.py requires events divisible by it)."""
        ident = {'task_id': self.ident['task_id'], 'panda_id': self.ident['job_id']}
        try:
            data = _server_get('get_event_range_statuses', {'job_task_ids': json.dumps([ident])}) or {}
        except Exception as exc:                              # noqa: BLE001
            self.lost_at = time.monotonic()
            logger.warning(f'range reconciliation failed: {exc}; again in {RECONCILE_DELAY_S} s')
            return
        statuses = data.get(str(ident['panda_id'])) or data.get(ident['panda_id']) or {}
        found, unknown = [], 0
        with self.cond:
            for rid, state in statuses.items():
                parts = str(rid).split('-')
                if (state or {}).get('status') != 'sent' or len(parts) != 5 or not parts[3].isdigit():
                    continue
                if self._was_received(parts):
                    continue
                fields = self.files.get(parts[2])
                if fields is None:
                    unknown += 1
                    continue
                start = int(parts[3])
                found.append(dict(fields, eventRangeID=rid, startEvent=start, lastEvent=start + self.span - 1))
            if found:
                self._note(found)
                self.ranges.extend(found)
                self.held += sum(range_span(r) for r in found)
                self.recovered += len(found)
            self.lost_at = None
            self.cond.notify_all()
        logger.warning(f'range reconciliation: PanDA holds {len(statuses)} ranges of this job; '
                       f'{len(found)} sent and never received are taken back'
                       + (f'; {unknown} of a file this pilot has no range of' if unknown else ''))

    def _fill(self):
        backoff = 0
        while not self.stopped.is_set():
            if self.lost_at is not None and time.monotonic() - self.lost_at >= RECONCILE_DELAY_S:
                self._reconcile()
            with self.cond:
                if self.exhausted or self.held >= self.size // 2:
                    self.cond.wait(1.0)
                    continue
                want = min(ACQUIRE_MAX, max(1, (self.size - self.held) // self.span))
                if not self.calls:
                    want = min(want, FIRST_ACQUIRE)     # learns the range size first
            try:
                got = _server('acquire_event_ranges', dict(self.ident, n_ranges=want)) or []
                backoff = 0
            except Exception as exc:                          # noqa: BLE001
                self.failures += 1
                if self.lost_at is None:
                    self.lost_at = time.monotonic()
                backoff = min(60, max(2, 2 * backoff))
                logger.warning(f'range prefetch failed ({self.failures}): {exc}; again in {backoff} s; '
                               f'reconciled with PanDA in {RECONCILE_DELAY_S} s')
                self.stopped.wait(backoff)
                continue
            with self.cond:
                self.calls += 1
                if got:
                    self._note(got)
                    self.ranges.extend(got)
                    self.held += sum(range_span(r) for r in got)
                    self.span = range_span(got[0])
                else:
                    self.exhausted = True
                    logger.info(f'range prefetch: the server has no more ranges after {self.calls} calls')
                self.cond.notify_all()

    def take(self, count):
        """Up to ``count`` ranges; none only when the server has none left and
        no failed acquisition awaits reconciliation, or when nothing has come
        for TAKE_GIVE_UP_S."""
        deadline = time.monotonic() + TAKE_GIVE_UP_S
        with self.cond:
            while (not self.ranges and (not self.exhausted or self.lost_at is not None)
                   and not self.stopped.is_set()):
                if time.monotonic() > deadline:
                    logger.error(f'range prefetch: nothing for {TAKE_GIVE_UP_S} s '
                                 f'({self.failures} failed calls); answering no more events')
                    break
                self.cond.wait(1.0)
            out = [self.ranges.popleft() for _ in range(min(count, len(self.ranges)))]
            self.held -= sum(range_span(r) for r in out)
            return out


def prefetch_start(process, job, stopped):
    """At the ES process's start, before the payload asks: a Prefetch on it
    when EPICPROD_ES_PREFETCH_EVENTS is set."""
    size = prefetch_size()
    if size:
        process._epic_prefetch = Prefetch(job, size, stopped)
        logger.info(f'range prefetch: holding up to {size} events, {ACQUIRE_MAX} ranges an acquisition')


def prefetch_take(process, max_ranges):
    """The payload's ranges from the prefetch, in get_event_range_to_payload's
    shape; None when the process has no prefetch."""
    prefetch = getattr(process, '_epic_prefetch', None)
    if prefetch is None:
        return None
    got = prefetch.take(max_ranges)
    if max_ranges == 1:
        return got[0] if got else []
    return got


def report_at_once():
    """EPICPROD_ES_REPORT_AT_ONCE=1: finished ranges go to the server as the
    payload reports them (each close as it stands), not at the stage-out gap."""
    return enabled() and os.environ.get('EPICPROD_ES_REPORT_AT_ONCE') == '1'


def report_now(executor, pending, final=False):
    """Send the queued unit reports straight to the server, whole units of up
    to REPORT_RANGES ranges a call. A call that gets no answer puts its units
    and those after it back at the front of the queue for the next pass, 30 s
    on; at the job's end (final) they are marked failed, as before. A range
    the server answers but does not take (already finished, or no longer this
    job's) is not sent again. The server takes a range update without a zip
    record, and fine-grained credit counts finished ranges (panda-server
    task_event_module.updateEventRanges, job_complex_module.check_fine_grained_processing)."""
    if not final and time.time() < getattr(executor, '_epic_report_retry_at', 0):
        return
    messages = []
    while pending:
        messages.append(pending.pop(0))
    batches, batch, size = [], [], 0
    for message in messages:
        n = len(message.get('ids', [message['id']]))
        if batch and size + n > REPORT_RANGES:
            batches.append(batch)
            batch, size = [], 0
        batch.append(message)
        size += n
    if batch:
        batches.append(batch)
    for i, batch in enumerate(batches):
        try:
            post_finished(executor, batch)
        except Exception as exc:                              # noqa: BLE001
            rest = [m for b in batches[i:] for m in b]
            if final:
                logger.error(f'final report of {len(rest)} messages failed: {exc}')
                executor.update_failed_event_ranges(rest)
            else:
                logger.warning(f'report of {len(rest)} messages failed: {exc}; again in 30 s')
                pending[:0] = rest
                executor._epic_report_retry_at = time.time() + 30
            return


def post_finished(executor, messages):
    ranges = [{'eventRangeID': rid, 'eventStatus': 'finished'}
              for message in messages for rid in message.get('ids', [message['id']])]
    data = _server('update_event_ranges', {'event_ranges': json.dumps([{'eventRanges': ranges}]), 'version': 1})
    returned = answers(data, len(ranges))
    if returned is None:
        raise RuntimeError(f'update_event_ranges answered {str(data)[:300]}')
    taken = sum(v is True for v in returned)
    if taken < len(ranges):
        logger.warning(f'the server took {taken} of {len(ranges)} finished ranges')
    executor.get_job().nevents += int(round(sum(
        e for e, v in zip(range_events(messages), returned) if v is True)))


# The job's log, economically (NERSC_PERLMUTTER.md, The job's log). Log test
# 41452's 149 MB were two thirds detector data copied into every slot and a
# third logs and per-unit records; the data and the records of units that
# succeeded leave (the harness record carries those), a failed unit stays whole,
# and a log keeps its head and its tail.
LOG_KEEP_HEAD = 64 * 1024
LOG_KEEP_TAIL = 192 * 1024
LOG_DATA_DIRS = ('run/calibrations', 'run/fieldmaps', 'run/gdml', 'handoff')


def cut_log(path, head=LOG_KEEP_HEAD, tail=LOG_KEEP_TAIL):
    """Keep a log's first ``head`` and last ``tail`` bytes, with a line saying
    how much was cut between them. Returns the bytes cut."""
    size = os.path.getsize(path)
    if size <= head + tail:
        return 0
    with open(path, 'rb') as handle:
        first = handle.read(head)
        handle.seek(size - tail)
        last = handle.read()
    gone = size - head - tail
    with open(path, 'wb') as handle:
        handle.write(first)
        handle.write(f'\n[... {gone} bytes cut for the job log (es_batch.trim_log_dir) ...]\n'.encode())
        handle.write(last)
    return gone


def trim_log_dir(workdir):
    """Reduce an Event Service job's work directory to its log before the
    pilot archives it: the harness's own files, every log cut to its head and
    tail, failed units whole; no detector data and no records of units that
    succeeded. A file that cannot be handled is said and left."""
    import shutil
    root = Path(workdir) / 'es_work'
    if not root.is_dir():
        return
    removed = cut = 0
    seen = set()                        # the slots' data copies are hard links of one another
    for slot in root.glob('slot*'):
        for name in LOG_DATA_DIRS:
            target = slot / name
            if target.exists():
                for f in target.rglob('*'):
                    if f.is_file() and not f.is_symlink():
                        st = f.stat()
                        if (st.st_dev, st.st_ino) not in seen:
                            seen.add((st.st_dev, st.st_ino))
                            removed += st.st_size
                shutil.rmtree(target, ignore_errors=True)
        for unit in (slot / 'outbox').glob('*'):
            if not unit.is_dir() or (unit / 'error.json').exists():
                continue
            for record in unit.glob('*.json'):
                try:
                    removed += record.stat().st_size
                    record.unlink()
                except OSError as exc:
                    logger.warning(f'job log: {record} not removed: {exc}')
    for path in root.rglob('*'):
        if path.is_file() and not path.is_symlink() and path.suffix in ('.log', '.txt', '.out', '.err'):
            try:
                cut += cut_log(path)
            except OSError as exc:
                logger.warning(f'job log: {path} not cut: {exc}')
    logger.info(f'job log: {removed} bytes of data and unit records removed, {cut} bytes cut from logs')


def site_input_dirs():
    """The directories where this site stages a job's inputs ahead of it
    (the launcher's EPICPROD_SITE_INPUT_DIRS, colon-separated): on Perlmutter
    the community file system copy (swf_epicprod/nersc_stage.py)."""
    return [d for d in os.environ.get('EPICPROD_SITE_INPUT_DIRS', '').split(':') if d]


def link_site_staged(files, workdir):
    """Inputs staged at the site, linked into the job's directory and marked
    delivered, so the pilot asks no catalog for a replica: a job's input
    resides wherever its catalog says, and the site's copy is made ahead of
    the job (docs/NERSC_PERLMUTTER.md, EVGEN input in production). A copy is
    taken only at the catalog's size. Returns the files left to the normal
    transfer."""
    dirs = site_input_dirs()
    if not dirs:
        return list(files)
    remaining = []
    for fspec in files:
        name = os.path.basename(fspec.lfn)
        staged = next((os.path.join(d, name) for d in dirs if os.path.isfile(os.path.join(d, name))), None)
        size = int(getattr(fspec, 'filesize', 0) or 0)
        if not staged or (size and os.path.getsize(staged) != size):
            logger.warning(f'input {fspec.lfn}: no copy at the site at its catalog size ({staged}); '
                           'left to the transfer')
            remaining.append(fspec)
            continue
        link = os.path.join(workdir, name)
        if os.path.lexists(link):
            os.unlink(link)
        os.symlink(staged, link)
        fspec.turl = 'file://' + staged
        fspec.status, fspec.status_code = 'transferred', 0
        logger.info(f'input {fspec.lfn}: the site copy {staged} linked as {link}')
    return remaining
