"""Preemption close-out (docs/NODE_EVENT_DISPATCHER.md, Preemption): an
Event Service job whose node is gone is closed by us in minutes, not
failed by the server's heartbeat timeout hours later.

The evidence is the record the harness ships off the node as it goes
(``reports/<PanDA job id>/es.json``, payload/es/es_record.py): written at
every unit, every close and at least every minute, so a record that has
been quiet for ``quiet_s`` on a running job means the node is gone. The
close-out then

1. credits the ranges of every close that stood (``update_event_ranges``,
   finished), which covers any the pilot had not yet passed on;
2. sends the job's final update (``update_job``, finished, with the
   job's attempt number and the credited count), so the server archives
   it through its fine-grained accounting: finished ranges counted, the
   rest released to the file for the next job, the job
   ``finished``/``fg_partial``.

Both are production-role calls, the role Harvester uses for lost
workers. Never a kill: ``killJob`` bypasses the fine-grained accounting.

A record is acted on only when it is a version 2 record (it carries the
harness's end marker), the harness has not ended (a normal end is
followed by the pilot's stage-out and reports, minutes of silence that
are no node loss), the record has not reached its write cap, and it has
been quiet for ``quiet_s``. The server-side call refuses a job that is
not running, is on another attempt or has moved to another queue.

Two sides, one module:

- the cycle (``run_cycle``) runs in the swf-monitor process as the
  production-operations agent's ``es_closeout_cycle`` doer (swf-monitor
  ``scripts/es-closeout-cycle.py``, five-minutely by cron enqueue): it
  reads the running Event Service jobs of the listed queues from the
  PanDA database and their records from the store, judges them, and
  records each close-out on the action stream;
- the PanDA calls (``finish_job``) run under the PanDA client
  environment, which holds the production token: the cycle runs this
  file there as a script (``--inside-pclient``), and
  ``tools/es_force_finish.py`` calls ``finish_job`` by hand. That side
  uses the standard library and ``pandaclient`` only.

Settings live in SysConfig under ``es_closeout.*``, seeded at their
defaults on first read so they show on the System page:

- ``es_closeout.enabled`` (True): the switch.
- ``es_closeout.queues`` (['BNL_NPPS_GPU']): the queues whose Event
  Service jobs are closed out when their node is lost.
- ``es_closeout.quiet_s`` (180): how long a record must be quiet.
"""
import json
import os
import subprocess
import sys
import tempfile
import time

PREEMPTED_CODE = 1256          # the pilot's "job killed: worker preempted / lost" family
CHUNK = 1000
RECORD_WRITE_CAP = 1500        # payload/es/es_record.py MAX_WRITES
RUNNING_STATES = ('running', 'starting', 'stagein', 'stageout')
DEFAULTS = {'enabled': True, 'queues': ['BNL_NPPS_GPU'], 'quiet_s': 180}
PCLIENT_SETUP = os.path.expanduser('~/pclient/run/setup.sh')
AUTH_VO = 'EIC.production'
PCLIENT_TIMEOUT_S = 180
ACTION_INSTANCE = 'ops-agent'


# The record

def closed_range_ids(record):
    """The distinct range ids of the units in closes that stood. A unit's
    first range id is <task>-<job>-<file>-<event>-<attempt>; its ranges are
    one event each over [startEvent, lastEvent]."""
    ids = []
    for u in record.get('done') or []:
        rng, uid = u.get('range') or {}, u.get('unit_id') or ''
        parts = uid.split('-')
        if len(parts) < 5 or rng.get('startEvent') is None:
            continue
        prefix, attempt = '-'.join(parts[:3]), parts[4]
        ids += [f'{prefix}-{e}-{attempt}' for e in range(int(rng['startEvent']), int(rng['lastEvent']) + 1)]
    return list(dict.fromkeys(ids))


def judge(record, age_s, quiet_s):
    """(act, reason) for one job's record, ``age_s`` since its last write."""
    if not isinstance(record, dict) or record.get('kind') != 'es_record':
        return False, 'not an Event Service record'
    if int(record.get('version') or 1) < 2:
        return False, 'record version 1 carries no end marker'
    if record.get('ended_at'):
        return False, 'the harness ended normally'
    if int(record.get('sequence') or 0) >= RECORD_WRITE_CAP - 1:
        return False, 'the record reached its write cap'
    if age_s < quiet_s:
        return False, f'record written {age_s:.0f} s ago'
    return True, f'record quiet {age_s:.0f} s'


# The PanDA side: standard library and pandaclient only

def _api(endpoint, data):
    import pandaclient.Client as C
    curl = C._Curl()
    curl.sslCert = C._x509()
    curl.sslKey = C._x509()
    return curl.post(f'{C.server_base_path_ssl}/{endpoint}', data, json_out=True)


def job_state(pandaid):
    """(status, attempt, queue) from the server. The final update must carry
    the attempt: without it the output report is filed under attempt 0, the
    adder drops it as the wrong attempt (adder_gen.py, process_job_report)
    and the job stays holding, and a second final update is ignored as
    already done (job_complex_module.py, updateJobStatus)."""
    import pandaclient.Client as C
    status, jobs = C.getJobStatus([pandaid])
    if status != 0 or not jobs or jobs[0] is None:
        raise RuntimeError(f'job {pandaid}: getJobStatus failed (status {status})')
    j = jobs[0]
    return j.jobStatus, int(j.attemptNr), j.computingSite


def _count_true(rets):
    """update_event_ranges returns its per-range results as text (six
    characters a range, "true, "), not a JSON list."""
    if isinstance(rets, str):
        low = rets.lower()
        return low.count('true'), low.count('true') + low.count('false')
    return sum(1 for x in rets if x is True), len(rets)


def finish_job(pandaid, ids, status='finished', expect_queue=None, expect_attempt=None,
               apply=True, say=print):
    """Credit ``ids`` and send the final update. Returns a result dict;
    ``ok`` is True only when the server took the final update."""
    out = {'pandaid': int(pandaid), 'ranges': len(ids), 'ok': False}
    job_status, attempt, queue = job_state(pandaid)
    out.update(job_status=job_status, attempt=attempt, queue=queue)
    if job_status not in RUNNING_STATES:
        out['refused'] = f'job is {job_status}: a final update would be ignored'
    elif expect_attempt is not None and int(expect_attempt) != attempt:
        out['refused'] = f'job is at attempt {attempt}, not {expect_attempt}'
    elif expect_queue and queue != expect_queue:
        out['refused'] = f'job is at {queue}, not {expect_queue}'
    if out.get('refused') or not apply:
        return out
    credited = 0
    for i in range(0, len(ids), CHUNK):
        batch = [{'eventRangeID': r, 'eventStatus': 'finished'} for r in ids[i:i + CHUNK]]
        http, resp = _api('event/update_event_ranges', {'event_ranges': json.dumps(batch), 'version': 0})
        ok = isinstance(resp, dict) and resp.get('success')
        rets = (((resp or {}).get('data') or {}).get('Returns') or []) if isinstance(resp, dict) else []
        n_true, n_all = _count_true(rets)
        credited += n_true
        say(f'job {pandaid} update_event_ranges {i}-{i + len(batch)}: http {http}, success {ok}, '
            f'{n_true} true of {n_all}'
            + ('' if ok else f', message {(resp or {}).get("message") if isinstance(resp, dict) else resp}'))
    out['credited'] = credited
    # nEvents is the pilot's last heartbeat count until told otherwise; the
    # fine-grained archive does not recompute it (job 3618959: 2,750 shown,
    # 6,750 ranges finished)
    data = {'job_id': int(pandaid), 'job_status': status, 'attempt_nr': attempt,
            'n_events': len(ids), 'pilot_error_code': PREEMPTED_CODE,
            'pilot_error_diag': 'Event Service node lost (preempted); force-finished from the record '
                                'the harness shipped: every close that stood credited'}
    http, resp = _api('pilot/update_job', data)
    out['update'] = resp if isinstance(resp, dict) else str(resp)
    out['ok'] = bool(isinstance(resp, dict) and resp.get('success'))
    say(f'job {pandaid} update_job {status}: http {http}, response {json.dumps(out["update"])[:400]}')
    return out


def _inside_pclient(payload_path):
    """Run the payload's close-outs; print one JSON line of results."""
    with open(payload_path) as f:
        payload = json.load(f)
    results = []
    for job in payload['jobs']:
        try:
            results.append(finish_job(job['pandaid'], job['ids'], expect_queue=job.get('queue'),
                                      expect_attempt=job.get('attempt'),
                                      say=lambda s: print(s, file=sys.stderr)))
        except Exception as e:                                # noqa: BLE001
            results.append({'pandaid': job['pandaid'], 'ok': False, 'error': f'{type(e).__name__}: {e}'})
    print(json.dumps({'results': results}))
    return 0


def finish_in_pclient(jobs, setup=PCLIENT_SETUP, auth_vo=AUTH_VO, timeout=PCLIENT_TIMEOUT_S):
    """Run ``finish_job`` for each of ``jobs`` ({pandaid, ids, queue,
    attempt}) under the PanDA client environment; returns the results.
    The token is the cached one; nothing here can start a device flow."""
    with tempfile.TemporaryDirectory(prefix='es-closeout-') as tmp:
        payload = os.path.join(tmp, 'payload.json')
        with open(payload, 'w') as f:
            json.dump({'jobs': jobs}, f)
        script = (f'source {setup} >/dev/null 2>&1\n'
                  f'export PANDA_AUTH_VO={auth_vo}\n'
                  f'python3 {os.path.abspath(__file__)} --inside-pclient {payload}\n')
        p = subprocess.run(['bash', '-c', script], capture_output=True, text=True,
                           timeout=timeout, stdin=subprocess.DEVNULL)
    lines = [ln for ln in (p.stdout or '').splitlines() if ln.startswith('{')]
    if p.returncode != 0 or not lines:
        raise RuntimeError(f'PanDA client step failed (exit {p.returncode}): '
                           f'{(p.stderr or p.stdout or "").strip()[-600:]}')
    return json.loads(lines[-1])['results'], p.stderr


# The cycle: runs in the swf-monitor process

def setting(key, seed=True):
    """The setting, seeded at its default on first read; a dry run reads
    without seeding, so it writes nothing."""
    from monitor_app.models import SysConfig
    if not seed:
        return SysConfig.get_config().get(f'es_closeout.{key}', DEFAULTS[key])
    return SysConfig.get_setting(f'es_closeout.{key}', DEFAULTS[key])


def candidates(queues):
    """Running Event Service jobs at the queues: [(pandaid, jeditaskid,
    queue, attempt)]."""
    from django.db import connections
    from monitor_app.panda.constants import PANDA_SCHEMA
    if not queues:
        return []
    marks = ', '.join(['%s'] * len(queues))
    sql = f"""
        SELECT "pandaid", "jeditaskid", "computingsite", "attemptnr"
        FROM "{PANDA_SCHEMA}"."jobsactive4"
        WHERE "eventservice" IS NOT NULL AND "eventservice" > 0
          AND "jobstatus" = 'running' AND "computingsite" IN ({marks})
    """
    with connections['panda'].cursor() as cursor:
        cursor.execute(sql, list(queues))
        return [(int(p), int(j) if j else None, q, int(a or 0)) for p, j, q, a in cursor.fetchall()]


def run_cycle(dry_run=False, created_by='es-closeout'):
    """One cycle. Returns (decisions, summary)."""
    from monitor_app.epicprod_logging import log_epicprod_action
    from monitor_app.payload_reports import sweeper_client

    t0 = time.monotonic()
    seed = not dry_run
    enabled, queues = setting('enabled', seed), setting('queues', seed)
    quiet_s = float(setting('quiet_s', seed))
    if not isinstance(queues, (list, tuple)) or not all(isinstance(q, str) for q in queues):
        raise ValueError(f'es_closeout.queues is not a list of queue names: {queues!r}')
    summary = {'enabled': bool(enabled), 'queues': list(queues), 'quiet_s': quiet_s,
               'jobs': 0, 'quiet': 0, 'closed': 0, 'failed': 0}
    decisions = []
    if enabled:
        store = sweeper_client()
        if store is None:
            raise RuntimeError('the report store credential is unusable')
        client, bucket, prefix = store
        to_close = []
        for pandaid, jeditaskid, queue, attempt in candidates(queues):
            summary['jobs'] += 1
            d = {'pandaid': pandaid, 'jeditaskid': jeditaskid, 'queue': queue, 'attempt': attempt}
            try:
                body = client.get_object(Bucket=bucket, Key=f'{prefix}{pandaid}/es.json')['Body'].read()
                record = json.loads(body.decode('utf-8'))
            except Exception as e:                            # noqa: BLE001
                d.update(act=False, reason='no record' if 'NoSuchKey' in str(e) else f'record unreadable: {e}')
                decisions.append(d)
                continue
            age = time.time() - float(record.get('written_at') or 0)
            act, reason = judge(record, age, quiet_s)
            d.update(act=act, reason=reason, record_age_s=round(age))
            if act:
                d['ids'] = closed_range_ids(record)
                to_close.append(d)
            decisions.append(d)
        summary['quiet'] = len(to_close)
        if to_close and not dry_run:
            try:
                results, _ = finish_in_pclient([{'pandaid': d['pandaid'], 'ids': d['ids'],
                                                 'queue': d['queue'], 'attempt': d['attempt']}
                                                for d in to_close])
            except Exception as e:                            # noqa: BLE001
                results = [{'pandaid': d['pandaid'], 'ok': False, 'error': str(e)} for d in to_close]
            by_id = {r['pandaid']: r for r in results}
            for d in to_close:
                r = by_id.get(d['pandaid'], {'ok': False, 'error': 'no result'})
                d['result'] = r
                ok = bool(r.get('ok'))
                summary['closed' if ok else 'failed'] += 1
                log_epicprod_action(
                    ACTION_INSTANCE, 'es_closeout', subject_type='panda_job',
                    subject_key=str(d['pandaid']), username=created_by,
                    outcome='ok' if ok else ('refused' if r.get('refused') else 'error'),
                    sublevel='normal', live_default=True,
                    message=(f"Event Service job {d['pandaid']} at {d['queue']}: {d['reason']}; "
                             + (f"closed, {len(d['ids'])} ranges of closes that stood credited"
                                if ok else f"not closed: {r.get('refused') or r.get('error') or r.get('update')}")),
                    ranges=len(d['ids']), jeditaskid=d['jeditaskid'] or 0,
                    record_age_s=d['record_age_s'])
    summary['duration_ms'] = int((time.monotonic() - t0) * 1000)
    if not dry_run:
        log_epicprod_action(
            ACTION_INSTANCE, 'es_closeout_cycle', subject_type='panda_queue',
            subject_key=','.join(queues), username=created_by,
            outcome='ok' if not summary['failed'] else 'error',
            duration_ms=summary['duration_ms'], sublevel='low', live_default=False,
            jobs=summary['jobs'], quiet=summary['quiet'], closed=summary['closed'],
            failed=summary['failed'])
    for d in decisions:
        d.pop('ids', None)
    return decisions, summary


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--inside-pclient':
        sys.exit(_inside_pclient(sys.argv[2]))
    print('usage: es_closeout.py --inside-pclient <payload.json>', file=sys.stderr)
    sys.exit(2)
