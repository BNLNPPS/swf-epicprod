#!/usr/bin/env python3
"""End tasks quietly (docs/EPICPROD_OPS.md, Quiet end): a commissioning test or
a stopped task is ended without a trace in the production record.

For each PanDA task: it must have a PCS task, must not have ended, and must
have no active or defined job (a running Event Service job ends quietly by the
close-out, tools/es_force_finish.py, which credits what it saved). The intent
is recorded first, ``ProdTask.overrides['quiet_end'] = {by, at, reason}``, so
the production readers leave the task out. PanDA's soft finish, which kills
nothing, is sent only when JEDI will end the task finished: at least one input
file finished and no fail-on-goal rule (FG). Otherwise JEDI would end it
failed ("failed since no file was successfully processed", 2026-10-27: tasks
41203 to 41316), so the task is left as it is in PanDA, paused, carrying the
marker alone. Recorded as a quiet_end action, off the live screen. Dry run
unless --apply.

Runs in the deployed monitor's environment on swf-testbed, on an operator's
word:

    /opt/swf-monitor/current/.venv/bin/python quiet_end.py <task id>... --reason TEXT [--apply]
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from es_credit_ended import deployed_environment  # noqa: E402

ENDED = ('done', 'finished', 'failed', 'broken', 'aborted', 'exhausted', 'prepared',
         'finishing', 'aborting', 'toabort')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('task_ids', type=int, nargs='+')
    ap.add_argument('--reason', required=True)
    ap.add_argument('--by', default=os.environ.get('USER', 'operator'))
    ap.add_argument('--apply', action='store_true')
    args = ap.parse_args()

    deployed_environment()
    from django.db import connections
    from django.utils import timezone
    from monitor_app.epicprod_logging import log_epicprod_action
    from pcs.models import PandaTasks
    from swf_epicprod import quiet_end

    plan, refused = [], []
    with connections['panda'].cursor() as cursor:
        for task_id in args.task_ids:
            link = PandaTasks.objects.select_related('prod_task').filter(jedi_task_id=task_id).first()
            cursor.execute('SELECT status FROM doma_panda.jedi_tasks WHERE jeditaskid=%s', [task_id])
            row = cursor.fetchone()
            jobs = 0
            for table in ('jobsactive4', 'jobsdefined4', 'jobswaiting4'):
                cursor.execute(f'SELECT count(*) FROM doma_panda.{table} WHERE jeditaskid=%s', [task_id])
                jobs += int(cursor.fetchone()[0])
            status = row[0] if row else None
            cursor.execute('''SELECT COALESCE(SUM(d.nfilesfinished), 0), MAX(t.splitrule)
                              FROM doma_panda.jedi_tasks t LEFT JOIN doma_panda.jedi_datasets d
                                ON d.jeditaskid = t.jeditaskid AND d.masterid IS NULL
                               AND d.type IN ('input', 'pseudo_input')
                              WHERE t.jeditaskid = %s''', [task_id])
            files_done, splitrule = cursor.fetchone() or (0, None)
            fail_on_goal = any(tok.split('=')[0] == 'FG' for tok in (splitrule or '').split(','))
            finish = bool(files_done) and not fail_on_goal
            why = ('no PanDA task' if row is None else
                   'no PCS task' if link is None else
                   f'already {status}' if status in ENDED else
                   f'{jobs} active or defined jobs (end a running ES job by the close-out)' if jobs else None)
            line = (f'{task_id} {status} jobs={jobs} files_finished={files_done} '
                    + ('PanDA finish' if finish else 'marker only, stays as it is in PanDA') + ' '
                    + (link.prod_task.composed_name if link else '-'))
            (refused if why else plan).append((task_id, link, finish, line + (f'  REFUSED: {why}' if why else '')))
    for *_, line in plan + refused:
        print(line)
    if refused:
        sys.exit(f'ERROR: {len(refused)} task(s) refused; nothing done')
    if not args.apply:
        print(f'dry run: {len(plan)} task(s) would end quietly; nothing done')
        return 0

    at = timezone.now().isoformat()
    for task_id, link, _, _ in plan:
        task = link.prod_task
        task.overrides = dict(task.overrides or {})
        task.overrides[quiet_end.MARKER] = {'by': args.by, 'at': at, 'reason': args.reason,
                                            'jedi_task_id': task_id}
        task.save(update_fields=['overrides'])
    quiet_end.invalidate()
    to_finish = [task_id for task_id, _, finish, _ in plan if finish]
    results = quiet_end.finish_in_pclient(to_finish) if to_finish else []
    results += [{'task_id': task_id, 'ok': True, 'response': 'marker only; PanDA state left as it is'}
                for task_id, _, finish, _ in plan if not finish]
    for r in results:
        print(f"{r['task_id']} finish: {'ok' if r.get('ok') else 'FAILED'} {r.get('response') or r.get('error')}")
        log_epicprod_action(
            'ops-agent', 'quiet_end', subject_type='panda_task', subject_key=str(r['task_id']),
            username=args.by, outcome='ok' if r.get('ok') else 'error', sublevel='low', live_default=False,
            message=f"Task {r['task_id']} ended quietly: {args.reason}", jeditaskid=r['task_id'])
    return 0 if all(r.get('ok') for r in results) else 1


if __name__ == '__main__':
    sys.exit(main())
