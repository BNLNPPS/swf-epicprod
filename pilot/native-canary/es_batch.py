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
    if len(set(ids)) != len(ids) or any(
            p[:3] + p[4:] != parts[0][:3] + parts[0][4:] or
            (i and p[3] != parts[i - 1][3] + 1) for i, p in enumerate(parts)):
        raise ValueError('ES result must be one contiguous owned unit')
    output = body.get('output')
    if not isinstance(output, str) or not output.startswith('/'):
        raise ValueError('ES result must name its absolute unit record')
    return {'id': ids[0], 'ids': ids, 'output': output, 'status': 'finished',
            'cpu': body.get('wall', 0), 'wall': body.get('wall', 0)}


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
    for first in range(0, len(ranges), API_BATCH_RANGES):
        batch = ranges[first:first + API_BATCH_RANGES]
        chunk = {'eventRanges': batch}
        if zip_file is not None:
            chunk['zipFile'] = dict(zip_file, numEvents=len(batch))
        response = executor.update_events({'version': 1, 'eventRanges': json.dumps([chunk])})
        returned = response.get('Returns') if isinstance(response, dict) else None
        if isinstance(returned, str):
            returned = json.loads(returned)
        # panda-server commits the range updates, then sets the job's nEvents;
        # on PostgreSQL that statement's :PandaID bind is missing from its
        # parameters (:pandaID; task_event_module.py updateEventRanges), and
        # the handler appends one False after the per-range answers. The
        # ranges are committed and their answers stand.
        if isinstance(returned, list) and len(returned) == len(batch) + 1 and returned[-1] is False:
            returned = returned[:-1]
        if not isinstance(returned, list) or len(returned) != len(batch) or any(v is not True for v in returned):
            raise RuntimeError('PanDA did not acknowledge the entire owned event batch')
    executor.get_job().nevents += len(ranges)


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
TAKE_GIVE_UP_S = 300        # an empty cache the server does not refill: answer "No more events"


def _server(endpoint, body):
    """One call to the server's event API, as the communicator makes it."""
    from pilot.util import https
    from pilot.util.config import config
    url = os.environ.get('PANDA_SERVER_URL', config.Pilot.pandaserver)
    res = https.request2(f'{url}/api/v1/event/{endpoint}', json_body=body, panda=True)
    if not isinstance(res, dict) or not res.get('success'):
        raise RuntimeError(f'{endpoint} refused: {str(res)[:300]}')
    return res.get('data')


def prefetch_size():
    """EPICPROD_ES_PREFETCH_RANGES: the ranges a job holds on the node; unset is off."""
    raw = os.environ.get('EPICPROD_ES_PREFETCH_RANGES', '')
    if not raw or not enabled():
        return 0
    if not raw.isdigit() or not 1000 <= int(raw) <= 1000000:
        raise ValueError('EPICPROD_ES_PREFETCH_RANGES must be 1000..1000000 on a batch pilot')
    return int(raw)


class Prefetch:
    """The job's ranges, acquired from its start and refilled whenever fewer
    than half of ``size`` are held, ACQUIRE_MAX a call, until the server has
    none left. Ranges a job holds when it ends are released by the server's
    fine-grained accounting with every other range it did not finish."""

    def __init__(self, job, size, stopped):
        self.ident = {'job_id': int(job.jobid), 'jobset_id': int(job.jobsetid), 'task_id': int(job.taskid)}
        self.size, self.stopped = size, stopped
        self.ranges = collections.deque()
        self.exhausted = False
        self.calls = self.failures = 0
        self.cond = threading.Condition()
        self.thread = threading.Thread(target=self._fill, name='es-prefetch', daemon=True)
        self.thread.start()

    def _fill(self):
        backoff = 0
        while not self.stopped.is_set():
            with self.cond:
                if self.exhausted or len(self.ranges) >= self.size // 2:
                    self.cond.wait(1.0)
                    continue
                want = min(ACQUIRE_MAX, self.size - len(self.ranges))
            try:
                got = _server('acquire_event_ranges', dict(self.ident, n_ranges=want)) or []
                backoff = 0
            except Exception as exc:                          # noqa: BLE001
                self.failures += 1
                backoff = min(60, max(2, 2 * backoff))
                logger.warning(f'range prefetch failed ({self.failures}): {exc}; again in {backoff} s')
                self.stopped.wait(backoff)
                continue
            with self.cond:
                self.calls += 1
                if got:
                    self.ranges.extend(got)
                else:
                    self.exhausted = True
                    logger.info(f'range prefetch: the server has no more ranges after {self.calls} calls')
                self.cond.notify_all()

    def take(self, count):
        """Up to ``count`` ranges; none only when the server has none left,
        or when it has refilled nothing for TAKE_GIVE_UP_S."""
        deadline = time.monotonic() + TAKE_GIVE_UP_S
        with self.cond:
            while not self.ranges and not self.exhausted and not self.stopped.is_set():
                if time.monotonic() > deadline:
                    logger.error(f'range prefetch: nothing for {TAKE_GIVE_UP_S} s '
                                 f'({self.failures} failed calls); answering no more events')
                    break
                self.cond.wait(1.0)
            return [self.ranges.popleft() for _ in range(min(count, len(self.ranges)))]


def prefetch_start(process, job, stopped):
    """At the ES process's start, before the payload asks: a Prefetch on it
    when EPICPROD_ES_PREFETCH_RANGES is set."""
    size = prefetch_size()
    if size:
        process._epic_prefetch = Prefetch(job, size, stopped)
        logger.info(f'range prefetch: holding up to {size} ranges, {ACQUIRE_MAX} an acquisition')


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
    returned = data.get('Returns') if isinstance(data, dict) else None
    if isinstance(returned, str):
        returned = json.loads(returned)
    # The PostgreSQL job event-count statement's trailing False (send_finished).
    if isinstance(returned, list) and len(returned) == len(ranges) + 1 and returned[-1] is False:
        returned = returned[:-1]
    if not isinstance(returned, list) or len(returned) != len(ranges):
        raise RuntimeError(f'update_event_ranges answered {str(data)[:300]}')
    taken = sum(v is True for v in returned)
    if taken < len(ranges):
        logger.warning(f'the server took {taken} of {len(ranges)} finished ranges')
    executor.get_job().nevents += taken
