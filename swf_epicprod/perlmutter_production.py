"""Replenished particle-gun production on one Perlmutter node
(docs/NERSC_PERLMUTTER.md, Production)."""
from .google_production import register_plan, reserve, task_inventory

QUEUE = 'NERSC_Perlmutter_epic_es'
EVENTS = 6000000
MARKER = 'perlmutter_production'
AUTHORITY = '01a17817-55fd-7551-be68-d05dbb102492'
# The four-hour shape of task 41262 (NERSC_PERLMUTTER.md, Event Service jobs
# on Perlmutter) with gun-sized units and closes: a 5 GeV neutron simulates
# in about 0.24 s and its RECO holds 82 KB, so a 120 s close of the node's
# 128 slots is about 5 GB and the work directory stays under the pilot's
# maxwdir. One job takes the whole task: nJobs is events // events_per_job.
SHAPE = dict(events_per_job=EVENTS, unit_events=500, close_s=120, deadline_s=14100,
             margin_s=600, memory_mb_per_core=1350, slots='128', pilot_cores=256,
             resident_sim=True, resident_unit=True, fast_merge=True, batch_channel=True,
             parallel_start=True, reco_pool=32, reco_async=True, report_barrier=True)
# JEDI states in which a task has, or is about to make, a job. A paused task
# makes none.
LIVE_TASK_STATES = ('registered', 'defined', 'assigning', 'ready', 'pending', 'scouting',
                    'running', 'topreprocess', 'preprocessing', 'prepared', 'throttled')


def reserve_sample():
    return reserve('pOP', EVENTS, SHAPE, QUEUE, MARKER, AUTHORITY)


def occupancy():
    """The queue's jobs and its other live tasks: anything there holds a new
    task, so the queue never runs two nodes."""
    from django.db import connections
    with connections['panda'].cursor() as cursor:
        cursor.execute('SELECT count(*) FROM doma_panda.jobsactive4 WHERE computingsite=%s', [QUEUE])
        jobs = cursor.fetchone()[0]
        cursor.execute('SELECT count(*) FROM doma_panda.jobsdefined4 WHERE computingsite=%s', [QUEUE])
        jobs += cursor.fetchone()[0]
        cursor.execute('SELECT jeditaskid FROM doma_panda.jedi_tasks WHERE site=%s AND status = ANY(%s)',
                       [QUEUE, list(LIVE_TASK_STATES)])
        tasks = [row[0] for row in cursor.fetchall()]
    return jobs, tasks


def replenish(created_by='es-closeout'):
    """Called by the ES close-out cycle, at most one submit per cycle."""
    from monitor_app.models import SysConfig
    if not SysConfig.get_setting('perlmutter_production.enabled', False):
        return {'outcome': 'disabled'}
    from monitor_app.cached_product import get_product
    from monitor_app.epicprod_logging import log_epicprod_action
    result = {'outcome': 'held', 'queue': QUEUE}
    try:
        from .queues import entries
        entry = next(e for e in entries() if e['queue'] == QUEUE)
        if entry['status'] != 'production':
            raise RuntimeError('Perlmutter ES queue is not in production')
        active, task = task_inventory(MARKER, 'Perlmutter')
        jobs, tasks = occupancy()
        result.update(active_tasks=active, queue_jobs=jobs, queue_tasks=tasks)
        if active or jobs or tasks:
            result.update(outcome='steady', reason='the node has its work')
        else:
            from pcs import services
            from pcs.commands import build_evgen_task_params
            from .front import full_output_rses, output_storage_problem
            from .gke_pilots import settings as gke_settings
            from .perlmutter_pilots import capability
            task = task or reserve_sample()
            problem = output_storage_problem(task.get_effective_config(), full_output_rses())
            if problem:
                raise RuntimeError(problem)
            spec = build_evgen_task_params(task)
            # A fresh read of the launcher, which submission admission reads too.
            images = capability(refresh=True).get('images') or []
            if spec['nJobs'] != 1 or spec['containerImage'] not in images:
                raise RuntimeError('Perlmutter task shape/image differs from the launcher')
            register_plan(task, gke_settings()['proxy'], MARKER)
            services.prodtask_submit_request(task=task, changed_by='wenaus',
                                             source=MARKER, site=QUEUE)
            result.update(outcome='submitted', reason='replenished particle-gun work',
                          task=task.composed_name, events=EVENTS, jobs=1,
                          authority=AUTHORITY)
    except Exception as exc:
        result['reason'] = f'{type(exc).__name__}: {exc}'
    get_product(MARKER, lambda: result, ttl_seconds=900, refresh=True)
    log_epicprod_action('ops-agent', MARKER, username=created_by,
                       outcome=result['outcome'], sublevel='normal',
                       live_default=result['outcome'] not in ('steady', 'disabled'),
                       message=f"Perlmutter production: {result['outcome']}: {result['reason']}",
                       **{k: v for k, v in result.items() if k not in ('outcome', 'reason')})
    return result
