"""Event Service range rows, removed three days after a task ends for good.

JEDI keeps a JEDI_Events row for every event range of every fine-grained
Event Service task and never deletes one: panda-server only marks rows,
and ATLAS clears them by dropping table partitions, while every ePIC task
sits in one partition. The rows steer a task's execution and its reruns to
completion; once the task is done or aborted, states JEDI does not retry,
they have no use beyond diagnosis, and they are the PanDA database's largest
table (Torre Wenaus, 2026-10-27, and 2026-10-29: "a standing 3 day cleanup").

A failed or finished task keeps its rows. A retry inserts new jobs that skip
the events its done rows record (panda-server job_complex_module, "get
successful event ranges"); without them it would run those events again.

Rows go in batches by primary key, each batch its own transaction, so the
PanDA server's own updates are never held behind one long delete. Each run
records one ``es_event_cleanup`` action.
"""
import logging
import time
from datetime import datetime, timedelta, timezone

from django.db import connections

logger = logging.getLogger(__name__)

SCHEMA = 'doma_panda'
DEFAULT_DAYS = 3
# Task states JEDI does not retry; any other state keeps its rows.
ENDED_FOR_GOOD = ('done', 'aborted')
BATCH = 50000
PAUSE_S = 0.2


def days():
    from monitor_app.models import SysConfig
    value = SysConfig.get_setting('es_event_cleanup.days', DEFAULT_DAYS)
    return value if isinstance(value, int) and value >= 1 else DEFAULT_DAYS


def candidates(older_than_days):
    """[(task, status, endtime)] of tasks ended for good before the cutoff
    that still hold event rows."""
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=older_than_days)
    with connections['panda'].cursor() as cursor:
        cursor.execute(f'''
            SELECT t.jeditaskid, t.status, t.endtime FROM {SCHEMA}.jedi_tasks t
            WHERE t.status = ANY(%s) AND t.endtime < %s
              AND EXISTS (SELECT 1 FROM {SCHEMA}.jedi_events e WHERE e.jeditaskid = t.jeditaskid)
            ORDER BY t.jeditaskid''', (list(ENDED_FOR_GOOD), cutoff))
        return cursor.fetchall()


def count(task):
    with connections['panda'].cursor() as cursor:
        cursor.execute(f'SELECT COUNT(*) FROM {SCHEMA}.jedi_events WHERE jeditaskid = %s', (task,))
        return int(cursor.fetchone()[0])


def delete_task(task):
    """Delete one task's rows batch by batch; returns the rows deleted.
    The status is read again first, so a task retried since it was listed
    keeps its rows."""
    removed = 0
    conn = connections['panda']
    while True:
        with conn.cursor() as cursor:
            cursor.execute(f'SELECT status FROM {SCHEMA}.jedi_tasks WHERE jeditaskid = %s', (task,))
            row = cursor.fetchone()
            if not row or row[0] not in ENDED_FOR_GOOD:
                logger.warning('task %s is now %s; its rows stay', task, row and row[0])
                return removed
            cursor.execute(f'''
                DELETE FROM {SCHEMA}.jedi_events
                WHERE (jeditaskid, pandaid, fileid, job_processid) IN (
                    SELECT jeditaskid, pandaid, fileid, job_processid FROM {SCHEMA}.jedi_events
                    WHERE jeditaskid = %s LIMIT %s)''', (task, BATCH))
            n = cursor.rowcount
        if not conn.get_autocommit():
            conn.commit()
        removed += n
        if n < BATCH:
            return removed
        time.sleep(PAUSE_S)


def run(dry_run=False, created_by='es_event_cleanup'):
    """List, and unless ``dry_run`` delete, the rows of tasks ended for good
    more than ``days()`` ago. Returns the run's record."""
    started = time.time()
    older = days()
    record = {'days': older, 'states': list(ENDED_FOR_GOOD), 'dry_run': dry_run,
              'tasks': [], 'rows': 0, 'error': None}
    try:
        for task, status, ended in candidates(older):
            n = count(task) if dry_run else delete_task(task)
            record['tasks'].append({'task': task, 'status': status, 'ended': ended.isoformat() + 'Z',
                                    'rows': n})
            record['rows'] += n
    except Exception as exc:  # noqa: BLE001
        logger.exception('ES event cleanup failed')
        record['error'] = f'{type(exc).__name__}: {exc}'
    record['seconds'] = round(time.time() - started, 1)
    if not dry_run:
        from monitor_app.epicprod_logging import log_epicprod_action
        log_epicprod_action(
            'ops-agent', 'es_event_cleanup', username=created_by,
            outcome='error' if record['error'] else 'ok',
            sublevel='normal' if record['error'] else 'low',
            subject_type='database', subject_key='jedi_events',
            message=(f"ES event rows: {record['rows']:,} removed from {len(record['tasks'])} "
                     f"done or aborted tasks ended over {older} days ago"
                     + (f"; stopped: {record['error']}" if record['error'] else '')),
            **{k: v for k, v in record.items() if k != 'tasks'},
            task_ids=[t['task'] for t in record['tasks']])
    return record
