"""Pause a production task whose completed jobs overwhelmingly fail.

The front reads and queues; the existing operations agent pauses and verifies.
An operator's verified resume starts a fresh sample. No automatic resume or kill.
"""

from datetime import timezone

MIN_COMPLETED = 50
FAILED_PERCENT = 95
SOURCE = 'front_task_failure_guard'
ACTIVE_STATUSES = ('ready', 'running', 'scouting', 'pending')


def should_pause(finished, failed):
    completed = finished + failed
    return completed >= MIN_COMPLETED and failed * 100 >= FAILED_PERCENT * completed


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
    return tasks


def run(queues, *, enabled, mode, dry_run, created_by):
    from monitor_app.epicprod_logging import log_epicprod_action
    from monitor_app.panda.operations import queue_task_operation
    from monitor_app.panda.queries import get_task_operation_targets

    results = []
    if not enabled:
        return results
    for sample in observations(queues):
        if not should_pause(sample['finished'], sample['failed']):
            continue
        evidence = dict(sample, min_completed=MIN_COMPLETED, failed_percent=FAILED_PERCENT)
        result = dict(evidence, outcome='would_pause')
        if mode == 'active' and not dry_run:
            # Recheck status at actuation; a person may have paused or finished it.
            current = get_task_operation_targets([sample['jeditaskid']])
            if not current or current[0]['status'] not in ACTIVE_STATUSES:
                result['outcome'] = 'state_changed'
            else:
                record, created = queue_task_operation(
                    task=current[0], operation='pause', requested_by=created_by,
                    source=SOURCE, evidence=evidence)
                result.update(outcome='pause_requested' if created else 'pause_pending',
                              operation_id=str(record.id))
                if created:
                    log_epicprod_action(
                        'front', 'task_failure_guard', subject_type='panda_task',
                        subject_key=str(sample['jeditaskid']), subject_label=sample['taskname'],
                        username=created_by, outcome='pause_requested', sublevel='high',
                        live_default=True,
                        message=(f"Pausing {sample['taskname']}: {sample['failed']} of "
                                 f"{sample['finished'] + sample['failed']} completed jobs failed"),
                        operation_id=str(record.id), **evidence)
        results.append(result)
    return results
