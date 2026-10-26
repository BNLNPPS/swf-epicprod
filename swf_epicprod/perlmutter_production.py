"""Replenished particle-gun production on one Perlmutter node
(docs/NERSC_PERLMUTTER.md, Production)."""
from .google_production import register_plan, reserve, task_inventory

QUEUE = 'NERSC_Perlmutter_epic_es'
EVENTS = 6000000
MARKER = 'perlmutter_production'
AUTHORITY = '01a17817-55fd-7551-be68-d05dbb102492'
# The four-hour shape of task 41262 (NERSC_PERLMUTTER.md, Event Service jobs
# on Perlmutter) with gun-sized units and closes: a 5 GeV neutron simulates
# in 0.26 s on Perlmutter and its RECO holds 156 KB, so 128 slots write about
# 78 MB/s, which four concurrent closes of about 35 MB/s each carry (job
# 6071469). One job takes the whole task: nJobs is events // events_per_job.
# Reconstruction at 0.10 s an event against 440 events a second simulated
# (0.289 s on 128 slots, job 6074509) keeps about 45 members busy: 48 shared
# members, about 2.85 GB private each.
SHAPE = dict(events_per_job=EVENTS, unit_events=500, close_s=120, close_concurrency=4,
             deadline_s=14100, margin_s=600, memory_mb_per_core=1350, slots='128',
             pilot_cores=256, resident_sim=True, resident_unit=True, fast_merge=True,
             batch_channel=True, parallel_start=True, reco_pool=48, reco_async=True,
             report_barrier=True)
# JEDI states in which a task has, or is about to make, a job. A paused task
# makes none.
LIVE_TASK_STATES = ('registered', 'defined', 'assigning', 'ready', 'pending', 'scouting',
                    'running', 'topreprocess', 'preprocessing', 'prepared', 'throttled')


class Closed(Exception):
    """The queue is closed: the cycle ends without a decision to post."""


def reserve_sample(extra=None):
    return reserve('pOP', EVENTS, dict(SHAPE, **(extra or {})), QUEUE, MARKER, AUTHORITY)


TRIAL_KEYS = ('preempt_at_s', 'preempt_pilot', 'preempt_first_job')


def trial():
    """A controlled preemption waiting for the next submission: SysConfig
    ``perlmutter_production.trial``, the shape keys it adds to one sample
    (production_es.py); {} when none is set."""
    from monitor_app.models import SysConfig
    value = SysConfig.get_setting('perlmutter_production.trial', {})
    return {k: v for k, v in value.items() if k in TRIAL_KEYS} if isinstance(value, dict) else {}


def shaped(task, extra):
    """The sample about to go with exactly this submission's trial keys, so a
    trial whose submission failed never rides a later sample's draft."""
    es = task.overrides['event_service']
    want = dict({k: v for k, v in es.items() if k not in TRIAL_KEYS}, **extra)
    if want != es:
        task.overrides['event_service'] = want
        task.save(update_fields=['overrides'])
    return task


def occupancy():
    """The queue's jobs and its other live tasks: anything there holds a new
    task, so the queue runs one node outside a trial."""
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
            # Closed, by an operator or by the allocation floor
            # (nersc_allocation): no additions, and nothing to post.
            result.update(outcome='closed', reason=entry['reason'] or entry['status'])
            raise Closed
        active, task = task_inventory(MARKER, 'Perlmutter')
        jobs, tasks = occupancy()
        result.update(active_tasks=active, queue_jobs=jobs, queue_tasks=tasks)
        # A trial goes in beside the running work, a node of its own, as a
        # whole production sample: once its first job is cut, the rest of the
        # sample runs as any other.
        extra = trial()
        if (active or jobs or tasks) and not extra:
            result.update(outcome='steady', reason='the node has its work')
        else:
            from pcs import services
            from pcs.commands import build_evgen_task_params
            from .front import full_output_rses, output_storage_problem
            from .gke_pilots import settings as gke_settings
            from .perlmutter_pilots import capability
            task = shaped(task or reserve_sample(extra), extra)
            problem =output_storage_problem(task.get_effective_config(), full_output_rses())
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
            if extra:
                SysConfig.update_config({'perlmutter_production.trial': {}}, username=created_by)
            result.update(outcome='submitted',
                          reason=f'controlled-preemption trial {extra}' if extra
                          else 'replenished particle-gun work',
                          task=task.composed_name, events=EVENTS, jobs=1,
                          authority=AUTHORITY)
    except Closed:
        pass
    except Exception as exc:
        result['reason'] = f'{type(exc).__name__}: {exc}'
    get_product(MARKER, lambda: result, ttl_seconds=900, refresh=True)
    log_epicprod_action('ops-agent', MARKER, username=created_by,
                       outcome=result['outcome'], sublevel='normal',
                       live_default=result['outcome'] not in ('steady', 'disabled', 'closed'),
                       message=f"Perlmutter production: {result['outcome']}: {result['reason']}",
                       **{k: v for k, v in result.items() if k not in ('outcome', 'reason')})
    return result
