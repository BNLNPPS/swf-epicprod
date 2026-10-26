"""Opt-in, bounded ES IPC batches; PanDA still receives its normal bulk protocol."""
import json
import os
import re
import tarfile
import time
from pathlib import Path

RESULT_PREFIX = 'EPICPROD_ES_RESULT_V1 '
MAX_MESSAGE_RANGES = 512
API_BATCH_RANGES = 10000


def enabled():
    return os.environ.get('EPICPROD_SWITCH_ES_BATCH_CHANNEL') == '1'


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


def report_final(executor, messages):
    """The job's last reports, on the critical path of its end: the finished
    ranges go straight to the server, without a receipt archive or its upload.
    The server takes a range update without a zip record; fine-grained credit
    counts finished ranges (panda-server task_event_module.updateEventRanges,
    job_complex_module.check_fine_grained_processing)."""
    send_finished(executor, messages, None)


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
