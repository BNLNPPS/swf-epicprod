"""Automatic return of a switched-off front feed (CONTINUOUS_PRODUCTION.md,
Feed guard).

A queue whose feed was switched off with an automatic return
(SysConfig ``front.queue.<queue>.feed_auto_on`` = ``exit78``) is switched back
on by the front cycle once its output copies have recovered: no exit-78
failure at the queue for CLEAN_MINUTES, with at least MIN_FINISHED finished
jobs over the same window so a quiet stretch cannot pass for a healthy one.
The return clears the switch's reason (the dashboard's Pressure front OFF)
and its criterion, and is posted live.
"""
from datetime import timedelta

from django.utils import timezone

# Today's (2026-10-26) clean gaps between GREX copy-failure bursts lasted 35
# to 55 minutes; a resume judged on 35 clean minutes failed again at once.
CLEAN_MINUTES = 90
MIN_FINISHED = 100
CRITERIA = ('exit78',)


def verdict(queue, now=None):
    """Exit-78 failures and finished jobs at the queue over the clean window."""
    from django.db import connections
    now = now or timezone.now()
    since = now - timedelta(minutes=CLEAN_MINUTES)
    with connections['panda'].cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) FILTER (WHERE exeerrorcode = 78), "
            "COUNT(*) FILTER (WHERE jobstatus = 'finished') "
            "FROM doma_panda.jobsarchived4 WHERE computingsite = %s AND endtime > %s "
            "AND processingtype = 'epicproduction'", [queue, since])
        exit78, finished = cursor.fetchone()
    return {'exit78': int(exit78 or 0), 'finished': int(finished or 0),
            'clean': not exit78 and (finished or 0) >= MIN_FINISHED}


def run(queues, *, dry_run=False, created_by='front'):
    """Switch back on each feed whose automatic-return criterion now holds;
    returns one row per queue under such a criterion."""
    from monitor_app.epicprod_logging import log_epicprod_action
    from monitor_app.models import SysConfig
    cfg = SysConfig.get_config()
    rows = []
    for queue in queues:
        criterion = cfg.get(f'front.queue.{queue}.feed_auto_on')
        if cfg.get(f'front.queue.{queue}.feed') is not False or criterion not in CRITERIA:
            continue
        v = verdict(queue)
        row = dict(v, queue=queue, criterion=criterion, switched_on=False)
        if v['clean'] and not dry_run:
            SysConfig.update_config({f'front.queue.{queue}.feed': True,
                                     f'front.queue.{queue}.feed_off_reason': '',
                                     f'front.queue.{queue}.feed_auto_on': ''}, username=created_by)
            row['switched_on'] = True
            log_epicprod_action(
                'front', 'feed_auto_on', subject_type='panda_queue', subject_key=queue,
                username=created_by, outcome='ok', sublevel='high', live_default=True,
                message=(f'{queue}: front feed switched back on: no exit-78 failure in the last '
                         f'{CLEAN_MINUTES} min, {v["finished"]} jobs finished'),
                **row)
        rows.append(row)
    return rows
