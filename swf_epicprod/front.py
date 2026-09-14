"""The pressure front's decision cycle (docs/CONTINUOUS_PRODUCTION.md,
The dispatcher): for each regulated queue, read the census, the gates
and the ordered ``ready`` backlog, decide feed or hold with a reason,
and record the decision on the action stream.

The cycle runs as the production-operations agent's ``front_cycle``
doer (swf-monitor ``scripts/front-cycle.py``), every five minutes by
cron enqueue. It holds no credential: a feed is the existing PCS submit
action, which enqueues the credentialed submitter. In shadow mode (the
first build, and the default) the cycle decides and records but never
submits; the decision it would have made reads ``would_feed``.

Settings live in SysConfig under ``front.*`` and are seeded at their
defaults on first read, so every knob is visible on the System page:

- ``front.enabled`` (False): the global switch.
- ``front.mode`` ('shadow'): shadow or active.
- ``front.queues``: the regulated queues.
- ``front.max_per_cycle`` (2): feeds per queue per cycle.
- ``front.activation_window_s`` (600): a feed counts as committed depth
  for this long before the census is expected to show it.
- ``front.queue.<queue>.feed`` (False), ``.h_low`` (8), ``.h_high``
  (24), ``.j_max`` (0 = none), ``.t_max`` (3), ``.breaker`` ('closed').

Decision records: one ``front_decision`` per queue per cycle when the
state or reason changed, on every feed, and hourly as a heartbeat;
one ``front_cycle`` per cycle. Each carries the observation time, the
depth in jobs and hours, the set points, the gates with their ages,
the breaker state, the candidate task and the reason code.
"""
import logging
from datetime import timedelta

from django.utils import timezone

logger = logging.getLogger(__name__)

DEFAULT_QUEUES = ['BNL_OSG_EPIC_PROD_1', 'UM_GREX_PanDA_1',
                  'NERSC_Perlmutter_epic', 'BNL_ePIC_GOOGLE']
QUEUE_DEFAULTS = {'feed': False, 'h_low': 8.0, 'h_high': 24.0,
                  'j_max': 0, 't_max': 3, 'breaker': 'closed'}
HEARTBEAT_S = 3600
CANARY_STALE_H = 24
CREDENTIAL_STALE_H = 36
# The fast detectors, until the alarm-queue modules exist.
BURN_MIN_FAILED = 10
BURN_FAST_FRACTION = 0.5
FAILURE_WINDOW_MIN_OUTCOMES = 50
FAILURE_WINDOW_RATE = 0.6
IDLE_FRACTION = 0.3


def setting(key, default):
    from monitor_app.models import SysConfig
    return SysConfig.get_setting(key, default)


def queue_settings(queue):
    out = {}
    for key, default in QUEUE_DEFAULTS.items():
        out[key] = setting(f'front.queue.{queue}.{key}', default)
    return out


def _canary_gate(queue):
    """The passive canary verdict for the queue and the age of its
    evidence window: red when failing or when the newest verdict's window
    ended more than CANARY_STALE_H ago."""
    from canary.store.models import Queue, Verdict
    row = Queue.objects.filter(name=queue).first()
    if row is None:
        return {'status': 'unknown', 'age_h': None, 'red': False,
                'reason': 'no canary record'}
    verdict = (Verdict.objects.filter(queue=row).order_by('-created_at')
               .values('evidence', 'created_at').first())
    age_h = None
    if verdict:
        window_end = (verdict.get('evidence') or {}).get('window_end')
        stamp = None
        if window_end:
            from django.utils.dateparse import parse_datetime
            stamp = parse_datetime(str(window_end))
        stamp = stamp or verdict.get('created_at')
        if stamp is not None:
            age_h = round((timezone.now() - stamp).total_seconds() / 3600.0, 1)
    status = str(row.status or 'unknown')
    red = status == 'failing' or (age_h is not None and age_h > CANARY_STALE_H)
    reason = ('canary failing' if status == 'failing'
              else f'canary evidence {age_h} h old' if red else '')
    return {'status': status, 'age_h': age_h, 'red': red, 'reason': reason}


def _credential_gate():
    """The nightly credential check's latest outcome and age."""
    from monitor_app.models import AppLog
    row = (AppLog.objects.filter(app_name='epicprod',
                                 extra_data__action='credential_expiry_check')
           .order_by('-timestamp').values('timestamp', 'extra_data').first())
    if not row:
        return {'outcome': 'none', 'age_h': None, 'red': True,
                'reason': 'no credential check on record'}
    age_h = round((timezone.now() - row['timestamp']).total_seconds() / 3600.0, 1)
    outcome = str((row['extra_data'] or {}).get('outcome') or '')
    red = outcome != 'ok' or age_h > CREDENTIAL_STALE_H
    reason = (f'credential check {outcome}' if outcome != 'ok'
              else f'credential check {age_h} h old' if red else '')
    return {'outcome': outcome, 'age_h': age_h, 'red': red, 'reason': reason}


def _fast_detectors(q):
    """Burn-through and the windowed failure rate from the census gate
    window (CONTINUOUS_PRODUCTION.md, The tripwire)."""
    g = q.get('gate') or {}
    finished, failed, fast = (int(g.get('finished') or 0), int(g.get('failed') or 0),
                              int(g.get('fast_failed') or 0))
    out = {'finished': finished, 'failed': failed, 'fast_failed': fast, 'red': False, 'reason': ''}
    if failed >= BURN_MIN_FAILED and fast / max(failed, 1) >= BURN_FAST_FRACTION and finished == 0:
        out.update(red=True, reason=f'burn-through: {fast} of {failed} failures fast, none finished')
        return out
    outcomes = finished + failed
    if outcomes >= FAILURE_WINDOW_MIN_OUTCOMES and failed / outcomes >= FAILURE_WINDOW_RATE:
        out.update(red=True, reason=f'failure window: {failed} of {outcomes} outcomes failed')
    return out


def _last_decision(queue):
    from monitor_app.models import AppLog
    return (AppLog.objects.filter(app_name='epicprod',
                                  extra_data__action='front_decision',
                                  extra_data__subject_key=queue)
            .order_by('-timestamp').values('timestamp', 'extra_data').first())


def _recent_feeds(queue, window_s):
    """The front's own feeds of the queue younger than the activation
    window: counted as committed depth until the census shows them."""
    from monitor_app.models import AppLog
    since = timezone.now() - timedelta(seconds=window_s)
    rows = (AppLog.objects.filter(app_name='epicprod',
                                  extra_data__action='front_decision',
                                  extra_data__subject_key=queue,
                                  extra_data__outcome__in=['fed', 'would_feed'],
                                  timestamp__gte=since)
            .values('extra_data'))
    return [r['extra_data'] for r in rows]


def ready_backlog():
    """The ``ready`` tasks by pinned queue, eligible ones first in
    priority order (level 1 first, unset last, then oldest), each with
    its declared rows and readiness problems."""
    from pcs.commands import (pinned_site, prodtask_manifest_rows,
                              prodtask_priority_level)
    from pcs.models import ProdTask
    from pcs.services import prodtask_readiness_problems
    by_queue = {}
    tasks = (ProdTask.objects.filter(status='ready')
             .select_related('dataset', 'request', 'prod_config', 'campaign')
             .order_by('created_at'))
    for task in tasks:
        cfg = task.get_effective_config()
        level, source = prodtask_priority_level(task)
        problems = prodtask_readiness_problems(task)
        rows = prodtask_manifest_rows(task, cfg=cfg) if not problems else None
        by_queue.setdefault(pinned_site(task, cfg=cfg), []).append({
            'task': task.composed_name, 'pk': task.pk,
            'level': level, 'level_source': source,
            'rows': rows, 'problems': problems,
            'created_at': task.created_at.isoformat(),
        })
    for entries in by_queue.values():
        entries.sort(key=lambda e: (bool(e['problems']), e['level'] or 9, e['created_at']))
    return by_queue


def decide_queue(queue, census_q, backlog, *, enabled, mode, max_per_cycle,
                 activation_window_s):
    """One queue's decision: the state, its reason code, and the record."""
    qs = queue_settings(queue)
    q = census_q or {}
    calib = q.get('calibration') or {}
    median_h = calib.get('median_walltime_h')
    ceiling = int(q.get('ceiling') or 0)
    not_started = int(q.get('not_started') or 0)
    running = int(q.get('running') or 0)
    runnable_h = q.get('hours_at_capacity')
    feeds = _recent_feeds(queue, activation_window_s)
    inflight_rows = sum(int(f.get('rows') or 0) for f in feeds)
    inflight_h = (round(inflight_rows * median_h / ceiling, 2)
                  if median_h and ceiling > 0 else 0.0)
    committed_h = (round((runnable_h or 0.0) + inflight_h, 2)
                   if runnable_h is not None else None)
    canary = _canary_gate(queue)
    credential = _credential_gate()
    fast = _fast_detectors(q)
    eligible = [e for e in backlog if not e['problems']]
    candidate = eligible[0] if eligible else None
    record = {
        'queue': queue, 'mode': mode,
        'not_started': not_started, 'running': running, 'ceiling': ceiling,
        'median_walltime_h': median_h, 'runnable_h': runnable_h,
        'inflight_feeds': len(feeds), 'inflight_h': inflight_h,
        'committed_h': committed_h,
        'h_low': qs['h_low'], 'h_high': qs['h_high'], 'j_max': qs['j_max'],
        't_max': qs['t_max'], 'feed_switch': bool(qs['feed']),
        'breaker': qs['breaker'],
        'canary': canary['status'], 'canary_age_h': canary['age_h'],
        'credential': credential['outcome'], 'credential_age_h': credential['age_h'],
        'gate_finished': fast['finished'], 'gate_failed': fast['failed'],
        'gate_fast_failed': fast['fast_failed'],
        'ready_total': len(backlog), 'ready_eligible': len(eligible),
        'candidate': candidate['task'] if candidate else '',
        'candidate_rows': candidate['rows'] if candidate else None,
        'candidate_level': candidate['level'] if candidate else None,
    }
    # The state machine, in the order of the plan's table.
    if not enabled:
        return 'held', 'front_off', record
    if not qs['feed']:
        return 'held', 'queue_off', record
    if str(qs['breaker']) != 'closed':
        return 'degraded', f"breaker_{qs['breaker']}", record
    if credential['red']:
        return 'degraded', 'credential', record
    if canary['red']:
        return 'degraded', 'canary', record
    if fast['red']:
        return 'degraded', ('burn_through' if fast['reason'].startswith('burn')
                            else 'failure_window'), record
    if feeds:
        return 'awaiting_observation', 'awaiting_observation', record
    if committed_h is None:
        return 'held', 'no_calibration', record
    if committed_h >= float(qs['h_low']):
        return 'supplied', 'supplied', record
    if ceiling > 0 and not_started > 0 and running < IDLE_FRACTION * ceiling:
        return 'idle_capacity', 'not_pulling', record
    if candidate is None:
        return 'no_work', 'no_eligible_task', record
    if candidate['rows'] and median_h and ceiling > 0:
        candidate_h = candidate['rows'] * median_h / ceiling
        if candidate_h > float(qs['h_high']):
            record['candidate_h'] = round(candidate_h, 2)
            return 'oversize', 'oversize_task', record
    if qs['j_max'] and not_started + int(candidate['rows'] or 0) > int(qs['j_max']):
        return 'held', 'job_cap', record
    outcome = 'would_feed' if mode != 'active' else 'fed'
    return outcome, f"fed:{candidate['task']}", record


def run_cycle(*, dry_run=False, created_by='front'):
    """One cycle over the regulated queues. Returns the per-queue
    decisions and the cycle summary; writes the records unless dry_run."""
    from monitor_app.epicprod_logging import log_epicprod_action
    from monitor_app.panda.census import queue_census

    t0 = timezone.now()
    enabled = bool(setting('front.enabled', False))
    mode = str(setting('front.mode', 'shadow'))
    queues = list(setting('front.queues', DEFAULT_QUEUES) or [])
    max_per_cycle = int(setting('front.max_per_cycle', 2))
    activation_window_s = int(setting('front.activation_window_s', 600))
    census = queue_census()
    backlog = ready_backlog()
    decisions = []
    written = 0
    for queue in queues:
        state, reason, record = decide_queue(
            queue, census['queues'].get(queue), backlog.get(queue, []),
            enabled=enabled, mode=mode, max_per_cycle=max_per_cycle,
            activation_window_s=activation_window_s)
        record.update(state=state, reason=reason,
                      observed_at=census['observed_at'])
        last = _last_decision(queue)
        last_extra = (last or {}).get('extra_data') or {}
        changed = (last is None or last_extra.get('reason') != reason
                   or last_extra.get('state') != state)
        stale = (last is not None and
                 (t0 - last['timestamp']).total_seconds() >= HEARTBEAT_S)
        is_feed = state in ('fed', 'would_feed')
        record['recorded'] = bool(is_feed or changed or stale)
        decisions.append(record)
        if dry_run or not record['recorded']:
            continue
        depth = (f"{record['committed_h']} h" if record['committed_h'] is not None
                 else 'no calibration')
        log_epicprod_action(
            'front', 'front_decision', subject_type='panda_queue',
            subject_key=queue, username=created_by, outcome=state,
            sublevel='normal' if is_feed else 'low', live_default=is_feed,
            message=(f'front {queue}: {state} ({reason}); committed {depth} '
                     f'of {record["h_low"]}-{record["h_high"]} h; '
                     f'{record["ready_eligible"]} eligible of '
                     f'{record["ready_total"]} ready'),
            **{k: v for k, v in record.items() if k != 'queue'})
        written += 1
    summary = {'observed_at': census['observed_at'], 'mode': mode,
               'enabled': enabled, 'queues': len(queues),
               'decisions_recorded': written,
               'states': {d['queue']: d['state'] for d in decisions},
               'duration_s': round((timezone.now() - t0).total_seconds(), 1)}
    if not dry_run:
        log_epicprod_action(
            'front', 'front_cycle', username=created_by, outcome='ok',
            duration_ms=int(summary['duration_s'] * 1000),
            sublevel='low', live_default=False,
            message=(f'front cycle ({mode}): '
                     + ', '.join(f'{q} {s}' for q, s in summary['states'].items())),
            **{k: v for k, v in summary.items() if k != 'states'})
    return decisions, summary
