# Event Service range rows: the three-day cleanup

A fine-grained Event Service task keeps one row in JEDI's `JEDI_Events`
table for every event range: ten events a row in production from 27
October, one event a row before. The rows steer the task's execution and
its reruns to completion. JEDI never deletes them: panda-server only marks
rows, and ATLAS clears them by dropping table partitions, while every ePIC
task sits in the table's first partition (task IDs below 1,000,000). On 29
October 2026 the table held 77 million rows in 21 GB, the largest in the
PanDA database, whose volume on pandadb01 had 52 GB of 150 GB free.

Once a task is done or aborted, states JEDI does not retry, its rows serve
only diagnosis, and they are removed three days after the task's end (Torre
Wenaus, 2026-10-27 and 2026-10-29). A failed or finished task keeps its
rows: a retry inserts new jobs that skip the events its done rows record
(panda-server `job_complex_module`, "get successful event ranges"), and
without them would run those events again.

`swf_epicprod/event_cleanup.py` lists the tasks done or aborted more than
`es_event_cleanup.days` days ago (SysConfig, default 3) that still hold rows,
and deletes each task's rows in batches of 50,000 by primary key, each
batch its own transaction, so the PanDA server's own updates never wait
behind one long delete. A task is read again before every batch; one
retried since it was listed keeps its rows. Each run records one
`es_event_cleanup` action with the rows removed and the task IDs.
Deleted rows free space inside the table for its new rows; the table's
files keep their size.

`scripts/es-event-cleanup.py` runs it (`--dry-run` lists the tasks and
their rows and deletes nothing), daily at 05:40 ET from the deployment
account's cron, under a lock that makes a second run exit:

```
40 5 * * * bash -lc 'source ~/.env && cd /opt/swf-monitor/current/src && /opt/swf-monitor/current/.venv/bin/python /data/wenauseic/github/swf-epicprod/scripts/es-event-cleanup.py' >> /opt/swf-monitor/shared/logs/es-event-cleanup.log 2>&1
```

The first run, at 03:06 ET on 29 October 2026, removed 1,953,920 rows of
66 tasks done or aborted more than three days before, in 62 seconds.
