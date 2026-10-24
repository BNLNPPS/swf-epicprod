"""Pause a production task whose completed jobs overwhelmingly fail.

The front reads and queues; the existing operations agent pauses and verifies.
An operator's verified resume starts a fresh sample. No automatic resume or kill.

Two rules (docs/CONTINUOUS_PRODUCTION.md, Task failure guard). The first,
50 completed attempts at 95% failed, acts as the front's mode says. The
rule mailed to the collaboration (Torre Wenaus, 2026-10-23) has its own
mode, shadow until it has run a day: a rolling sample of the latest
completed attempts, worker losses set aside, failures across hosts.
"""

from datetime import timezone

MIN_COMPLETED = 50
FAILED_PERCENT = 95
SOURCE = 'front_task_failure_guard'
ACTIVE_STATUSES = ('ready', 'running', 'scouting', 'pending')

# The mailed rule's defaults; each is a SysConfig setting
# front.task_guard.<name> (rolling_settings).
ROLLING = {'run_failed': 10, 'window': 50, 'window_failed': 25, 'min_hosts': 3}
ROLLING_SOURCE = 'front_task_failure_guard_rolling'
# A failure whose worker ended under the job counts as neither failure nor
# success: the glidein's, not the task's, and it arrives in batches that
# look exactly like a cascade. So does a job the pilot cut at the slot's
# time limit (Torre Wenaus, 2026-10-24): the slot's clock, not the task's
# fault, which on 2026-10-23 put 83 kills on 26 hosts at SU-ITS into a
# healthy task (41051).
WORKER_LOSS = (('jobdispatchererrorcode', 100),   # lost heartbeat
               ('taskbuffererrorcode', 300),      # worker finished while the job ran
               ('piloterrorcode', 1213))          # reached maxtime
# Attempts read per task, newest first: the window and room for the
# worker losses set aside within it.
ROLLING_READ = 300


def should_pause(finished, failed):
    completed = finished + failed
    return completed >= MIN_COMPLETED and failed * 100 >= FAILED_PERCENT * completed


def rolling_settings(setting=None):
    """The mailed rule's thresholds, each overridable by SysConfig."""
    if setting is None:
        return dict(ROLLING)
    out = {}
    for name, default in ROLLING.items():
        try:
            out[name] = int(setting(f'front.task_guard.{name}', default))
        except (TypeError, ValueError):
            out[name] = default
    return out


def judge_rolling(attempts, cfg=None):
    """The mailed rule over one task's completed attempts, newest first,
    each ``{'failed': bool, 'worker_loss': bool, 'host': str}``: a pause
    when the latest ``run_failed`` counted attempts all failed, or when
    ``window_failed`` of the latest ``window`` did, the failures behind
    the trigger spanning at least ``min_hosts`` hosts (a run on one node
    is the node guard's business). Returns ``(trip, evidence)``."""
    cfg = cfg or ROLLING
    counted, set_aside = [], 0
    for attempt in attempts:
        if len(counted) >= cfg['window']:
            break
        if attempt['worker_loss']:
            set_aside += 1
        else:
            counted.append(attempt)
    streak = []
    for attempt in counted:
        if not attempt['failed']:
            break
        streak.append(attempt)
    failures = [a for a in counted if a['failed']]
    run = streak[:cfg['run_failed']]
    run_hosts = len({a['host'] for a in run})
    window_hosts = len({a['host'] for a in failures})
    evidence = {'counted': len(counted), 'consecutive_failed': len(streak),
                'window_failed': len(failures), 'run_hosts': run_hosts,
                'window_hosts': window_hosts, 'set_aside': set_aside, **cfg}
    if len(run) >= cfg['run_failed'] and run_hosts >= cfg['min_hosts']:
        return True, dict(evidence, trigger='consecutive')
    if (len(counted) >= cfg['window'] and len(failures) >= cfg['window_failed']
            and window_hosts >= cfg['min_hosts']):
        return True, dict(evidence, trigger='window')
    return False, dict(evidence, trigger='')


def attempt_rows(rows):
    """PanDA job rows (jobstatus, jobdispatchererrorcode,
    taskbuffererrorcode, piloterrorcode, modificationhost), newest first,
    as the judge's attempts: the host without its slot
    (``slot1_2@comphy.fsu.edu`` -> ``comphy.fsu.edu``)."""
    out = []
    for status, dispatcher, taskbuffer, pilot, host in rows:
        codes = {'jobdispatchererrorcode': int(dispatcher or 0),
                 'taskbuffererrorcode': int(taskbuffer or 0),
                 'piloterrorcode': int(pilot or 0)}
        failed = status == 'failed'
        out.append({'failed': failed,
                    'worker_loss': failed and any(codes[k] == v for k, v in WORKER_LOSS),
                    'host': str(host or '').rsplit('@', 1)[-1]})
    return out


def observations(queues):
    """Read live tasks and distinct terminal job attempts; propagate read errors."""
    from django.db import connections
    from django.db.models import Max
    from monitor_app.models import PandaTaskOperation
    from monitor_app.panda.constants import PANDA_SCHEMA

    if not queues:
        return []
    with connections['panda'].cursor() as cursor:
        cursor.execute(
            f'SELECT jeditaskid, taskname, status, site FROM "{PANDA_SCHEMA}".jedi_tasks '
            'WHERE site = ANY(%s) AND status = ANY(%s) '
            "AND taskname LIKE %s AND prodsourcelabel = ANY(%s)",
            [list(queues), list(ACTIVE_STATUSES), 'group.EIC.%', ['managed', 'test']])
        tasks = [dict(zip(('jeditaskid', 'taskname', 'status', 'site'), row))
                 for row in cursor.fetchall()]
    if not tasks:
        return []
    resumes = {row['jedi_task_id']: row['since'] for row in
               PandaTaskOperation.objects.filter(
                   jedi_task_id__in=[t['jeditaskid'] for t in tasks],
                   operation='resume', status='verified', completed_at__isnull=False)
               .values('jedi_task_id').annotate(since=Max('completed_at'))}
    with connections['panda'].cursor() as cursor:
        for task in tasks:
            since = resumes.get(task['jeditaskid'])
            # PanDA timestamps are UTC without a timezone. A verified resume
            # excludes the old failure sample and jobs that finished while paused.
            cutoff = since.astimezone(timezone.utc).replace(tzinfo=None) if since else None
            predicate = 'jeditaskid=%s AND jobstatus IN (\'finished\',\'failed\')'
            params = [task['jeditaskid']]
            if cutoff is not None:
                predicate += ' AND endtime >= %s'
                params.append(cutoff)
            cursor.execute(
                'SELECT jobstatus, count(*) FROM ('
                f'SELECT pandaid, jobstatus FROM "{PANDA_SCHEMA}".jobsactive4 WHERE {predicate} '
                'UNION '
                f'SELECT pandaid, jobstatus FROM "{PANDA_SCHEMA}".jobsarchived4 WHERE {predicate}'
                ') jobs GROUP BY jobstatus', params + params)
            counts = dict(cursor.fetchall())
            task.update(finished=counts.get('finished', 0), failed=counts.get('failed', 0),
                        since=since.isoformat() if since else None)
            # The rolling sample: the latest completed attempts in completion
            # order, each PandaID once.
            cols = ('pandaid, jobstatus, endtime, jobdispatchererrorcode, taskbuffererrorcode, '
                    'piloterrorcode, modificationhost')
            cursor.execute(
                'SELECT jobstatus, jobdispatchererrorcode, taskbuffererrorcode, piloterrorcode, '
                'modificationhost FROM ('
                f'SELECT {cols} FROM "{PANDA_SCHEMA}".jobsactive4 WHERE {predicate} '
                'UNION '
                f'SELECT {cols} FROM "{PANDA_SCHEMA}".jobsarchived4 WHERE {predicate}'
                ') jobs ORDER BY endtime DESC NULLS LAST, pandaid DESC LIMIT %s',
                params + params + [ROLLING_READ])
            task['attempts'] = attempt_rows(cursor.fetchall())
    return tasks


def _shadow_recorded(jeditaskid, hours=24):
    """True when the rolling rule's shadow trip for this task was already
    recorded within ``hours``: one record per trip, not one per cycle."""
    from datetime import timedelta
    from django.utils import timezone as dj_timezone
    from monitor_app.models import AppLog
    return AppLog.objects.filter(
        timestamp__gte=dj_timezone.now() - timedelta(hours=hours),
        extra_data__action='task_failure_guard_shadow',
        extra_data__subject_key=str(jeditaskid)).exists()


def _pause(sample, evidence, *, source, created_by, message):
    """Queue a verified pause through the operations agent; the outcome."""
    from monitor_app.epicprod_logging import log_epicprod_action
    from monitor_app.panda.operations import queue_task_operation
    from monitor_app.panda.queries import get_task_operation_targets

    # Recheck status at actuation; a person may have paused or finished it.
    current = get_task_operation_targets([sample['jeditaskid']])
    if not current or current[0]['status'] not in ACTIVE_STATUSES:
        return {'outcome': 'state_changed'}
    record, created = queue_task_operation(
        task=current[0], operation='pause', requested_by=created_by,
        source=source, evidence=evidence)
    if created:
        log_epicprod_action(
            'front', 'task_failure_guard', subject_type='panda_task',
            subject_key=str(sample['jeditaskid']), subject_label=sample['taskname'],
            username=created_by, outcome='pause_requested', sublevel='high',
            live_default=True, message=message,
            operation_id=str(record.id), **evidence)
    return {'outcome': 'pause_requested' if created else 'pause_pending',
            'operation_id': str(record.id)}


def run(queues, *, enabled, mode, dry_run, created_by,
        rolling_mode='shadow', rolling_cfg=None, legacy=True):
    """One front cycle of the guard. ``mode`` is the front's; the first
    rule acts in it (``legacy``). The mailed rule acts only when
    ``rolling_mode`` is also ``active``; in ``shadow`` it records each
    trip once a day as task_failure_guard_shadow and acts on nothing;
    ``off`` skips it. Once the mailed rule acts, the first stays on as
    the backstop for total failure where a queue reports few host names
    (Torre Wenaus, 2026-10-24)."""
    from monitor_app.epicprod_logging import log_epicprod_action

    results = []
    if not enabled:
        return results
    acting = mode == 'active' and not dry_run
    cfg = rolling_cfg or ROLLING
    for sample in observations(queues):
        attempts = sample.pop('attempts', [])
        legacy_trip = legacy and should_pause(sample['finished'], sample['failed'])
        if legacy_trip:
            evidence = dict(sample, rule='legacy', min_completed=MIN_COMPLETED,
                            failed_percent=FAILED_PERCENT)
            result = dict(evidence, outcome='would_pause')
            if acting:
                result.update(_pause(
                    sample, evidence, source=SOURCE, created_by=created_by,
                    message=(f"Pausing {sample['taskname']}: {sample['failed']} of "
                             f"{sample['finished'] + sample['failed']} completed jobs failed")))
            results.append(result)
        if rolling_mode not in ('shadow', 'active'):
            continue
        trip, rolling = judge_rolling(attempts, cfg)
        if not trip:
            continue
        evidence = dict(sample, rule='rolling', **rolling)
        what = (f"{rolling['consecutive_failed']} consecutive completed attempts failed"
                if rolling['trigger'] == 'consecutive' else
                f"{rolling['window_failed']} of the latest {rolling['counted']} completed attempts failed")
        message = (f"{sample['taskname']}: {what}, across {rolling['run_hosts'] if rolling['trigger'] == 'consecutive' else rolling['window_hosts']} hosts"
                   f" ({rolling['set_aside']} worker losses set aside)")
        result = dict(evidence, outcome='would_pause')
        if rolling_mode == 'active' and acting and not legacy_trip:
            result.update(_pause(sample, evidence, source=ROLLING_SOURCE,
                                 created_by=created_by, message='Pausing ' + message))
        elif not dry_run and not _shadow_recorded(sample['jeditaskid']):
            # The shadow day's record: what the mailed rule would pause.
            log_epicprod_action(
                'front', 'task_failure_guard_shadow', subject_type='panda_task',
                subject_key=str(sample['jeditaskid']), subject_label=sample['taskname'],
                username=created_by, outcome='would_pause', sublevel='normal',
                live_default=False, message='Would pause ' + message, **evidence)
        results.append(result)
    return results
