"""The pressure front's decision cycle (docs/CONTINUOUS_PRODUCTION.md,
The dispatcher): for each regulated queue, read the census, the gates
and the ordered ``ready`` backlog, decide feed or hold with a reason,
and record the decision on the action stream.

The cycle runs as the production-operations agent's ``front_cycle``
doer (swf-monitor ``scripts/front-cycle.py``), every five minutes by
cron enqueue. It holds no credential: a feed is the existing PCS submit
action, which enqueues the credentialed submitter. In shadow mode (the
first build, and the only mode until the feed is built) the cycle
decides and records but never submits; the decision it would have made
reads ``would_feed``.

Settings live in SysConfig under ``front.*`` and are seeded at their
defaults on first read, so every knob is visible on the System page:

- ``front.enabled`` (False): the global switch.
- ``front.mode`` ('shadow'): shadow, or active once the feed exists.
- ``front.queues``: the regulated queues.
- ``front.max_per_cycle`` (2): feeds per queue per cycle.
- ``front.activation_window_s`` (600): a feed counts as committed depth
  for this long before the census is expected to show it.
- ``front.queue.<queue>.feed`` (False), ``.h_low`` (8), ``.h_high``
  (24), ``.j_max`` (0 = none), ``.t_max`` (3), ``.breaker`` ('closed').

Decision records: one ``front_decision`` per feed, one per queue when
the queue's state or reason changed, and hourly as a heartbeat; one
``front_cycle`` per cycle. Each carries the observation time, the depth
in jobs and hours, the set points, the gates with their ages, the
breaker state, the candidate task and the reason code. The cycle also
stores its whole state (every queue's latest decision, the backlog, the
errors) as the cached product ``front_state``, which the front page
reads; the page never computes.

Every read the cycle makes is fenced: a failed census, gate, backlog or
decision is recorded as such for the queue it concerns and the cycle
goes on. A failed read of a gate reads red.
"""
import hashlib
import logging
from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

DEFAULT_QUEUES = ['BNL_OSG_EPIC_PROD_1', 'UM_GREX_PanDA_1',
                  'NERSC_Perlmutter_epic', 'BNL_ePIC_GOOGLE']
QUEUE_DEFAULTS = {'feed': False, 'h_low': 8.0, 'h_high': 24.0,
                  'j_max': 0, 't_max': 3, 'breaker': 'closed'}
MODES = ('shadow',)
HEARTBEAT_S = 3600
CANARY_STALE_H = 24
CREDENTIAL_STALE_H = 36
# The fast detectors, until the alarm-queue modules exist.
BURN_MIN_FAILED = 10
BURN_FAST_FRACTION = 0.5
FAILURE_WINDOW_MIN_OUTCOMES = 50
FAILURE_WINDOW_RATE = 0.6
IDLE_FRACTION = 0.3
ROWS_CACHE_TTL_S = 24 * 3600
STATE_KEY = 'front_state'
STATE_TTL_S = 24 * 3600
FEED_STATES = ('fed', 'would_feed')


def _safe(label, fn, fallback):
    """Run ``fn``; on any exception log it and return ``fallback``
    (called with the exception when callable)."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        logger.exception('front: %s failed', label)
        return fallback(exc) if callable(fallback) else fallback


def setting(key, default):
    from monitor_app.models import SysConfig
    return SysConfig.get_setting(key, default)


def queue_settings(queue):
    out = {}
    for key, default in QUEUE_DEFAULTS.items():
        out[key] = setting(f'front.queue.{queue}.{key}', default)
    return out


def regulated_queues():
    """``front.queues`` as a list of names; anything else falls back to
    the defaults with an error logged."""
    value = setting('front.queues', DEFAULT_QUEUES)
    if isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
        return [v for v in value if v]
    logger.error('front.queues is not a list of queue names: %r; using the defaults', value)
    return list(DEFAULT_QUEUES)


# Gates

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


def fast_detectors(gate):
    """Burn-through and the windowed failure rate from the census gate
    window (CONTINUOUS_PRODUCTION.md, The tripwire). Pure."""
    g = gate or {}
    finished, failed, fast = (int(g.get('finished') or 0), int(g.get('failed') or 0),
                              int(g.get('fast_failed') or 0))
    out = {'finished': finished, 'failed': failed, 'fast_failed': fast,
           'red': False, 'reason': '', 'code': ''}
    if failed >= BURN_MIN_FAILED and fast / max(failed, 1) >= BURN_FAST_FRACTION and finished == 0:
        out.update(red=True, code='burn_through',
                   reason=f'burn-through: {fast} of {failed} failures fast, none finished')
        return out
    outcomes = finished + failed
    if outcomes >= FAILURE_WINDOW_MIN_OUTCOMES and failed / outcomes >= FAILURE_WINDOW_RATE:
        out.update(red=True, code='failure_window',
                   reason=f'failure window: {failed} of {outcomes} outcomes failed')
    return out


# The record

def _last_decision(queue):
    from monitor_app.models import AppLog
    return (AppLog.objects.filter(app_name='epicprod',
                                  extra_data__action='front_decision',
                                  extra_data__subject_key=queue)
            .order_by('-timestamp').values('id', 'timestamp', 'extra_data').first())


def _recent_feeds(queue, window_s):
    """The front's own feeds of the queue younger than the activation
    window: counted as committed depth until the census shows them."""
    from monitor_app.models import AppLog
    since = timezone.now() - timedelta(seconds=window_s)
    rows = (AppLog.objects.filter(app_name='epicprod',
                                  extra_data__action='front_decision',
                                  extra_data__subject_key=queue,
                                  extra_data__outcome__in=list(FEED_STATES),
                                  timestamp__gte=since)
            .values('extra_data'))
    return [r['extra_data'] for r in rows]


def _last_feed_age_h(queue):
    """Hours since the front's latest feed of the queue, None when it
    never fed it: the clock of the idle-capacity rule."""
    from monitor_app.models import AppLog
    row = (AppLog.objects.filter(app_name='epicprod',
                                 extra_data__action='front_decision',
                                 extra_data__subject_key=queue,
                                 extra_data__outcome__in=list(FEED_STATES))
           .order_by('-timestamp').values('timestamp').first())
    if not row:
        return None
    return round((timezone.now() - row['timestamp']).total_seconds() / 3600.0, 2)


# The backlog

def task_rows(task, cfg):
    """The jobs a first submission of the task declares, cached a day by
    task, per-job count and matched inputs, since the count comes from a
    read of the input catalog."""
    from pcs.commands import prodtask_events_per_job, prodtask_manifest_rows
    n_events = prodtask_events_per_job(task, cfg=cfg)
    if n_events <= 0:
        return None
    dids = sorted(str((i or {}).get('did') or '') for i in (task.inputs or []))
    digest = hashlib.sha1('|'.join(dids).encode()).hexdigest()[:12]
    key = f'front:rows:{task.pk}:{n_events}:{digest}'
    cached = cache.get(key)
    if cached is not None:
        return cached
    rows = prodtask_manifest_rows(task, cfg=cfg)
    if rows is not None:
        cache.set(key, rows, ROWS_CACHE_TTL_S)
    return rows


def ready_backlog():
    """The ``ready`` tasks by pinned queue, eligible ones first in
    priority order (level 1 first, unset last, then oldest), each with
    its declared rows and readiness problems. A task whose reading
    fails is carried with the failure as its problem."""
    from pcs.commands import pinned_site, prodtask_priority_level
    from pcs.models import ProdTask
    from pcs.services import prodtask_readiness_problems
    by_queue = {}
    tasks = (ProdTask.objects.filter(status='ready')
             .select_related('dataset', 'request', 'prod_config', 'campaign')
             .order_by('created_at'))
    for task in tasks:
        entry = {'task': task.composed_name, 'pk': task.pk, 'level': None,
                 'level_source': 'none', 'rows': None, 'problems': [],
                 'created_at': task.created_at.isoformat()}
        queue = 'unknown'
        try:
            cfg = task.get_effective_config()
            queue = pinned_site(task, cfg=cfg)
            entry['level'], entry['level_source'] = prodtask_priority_level(task)
            entry['problems'] = list(prodtask_readiness_problems(task))
            if not entry['problems']:
                entry['rows'] = task_rows(task, cfg)
        except Exception as exc:  # noqa: BLE001
            logger.exception('front: reading ready task %s failed', task.pk)
            entry['problems'].append(f'reading the task failed: {type(exc).__name__}: {exc}')
        by_queue.setdefault(queue, []).append(entry)
    for entries in by_queue.values():
        entries.sort(key=lambda e: (bool(e['problems']), e['level'] or 9, e['created_at']))
    return by_queue


# The decision

def decide(queue, census_q, backlog, settings, gates, feeds, *, enabled, mode,
           max_per_cycle, last_feed_age_h):
    """One queue's decisions, pure: a list of ``(state, reason, record)``,
    one per feed, else one for the queue's state.

    ``census_q`` is the census's entry for the queue (or None), ``backlog``
    the queue's ready entries, ``settings`` the queue's ``front.queue.*``
    values, ``gates`` the canary and credential gates, ``feeds`` the
    front's feeds younger than the activation window, ``last_feed_age_h``
    the hours since its latest feed (None when it never fed the queue).
    """
    qs = settings
    q = census_q or {}
    calib = q.get('calibration') or {}
    median_h = calib.get('median_walltime_h')
    p90_start_h = calib.get('p90_start_latency_h')
    ceiling = int(q.get('ceiling') or 0)
    not_started = int(q.get('not_started') or 0)
    running = int(q.get('running') or 0)
    tasks_active = int(q.get('tasks_active') or 0)
    runnable_h = q.get('hours_at_capacity')
    inflight_rows = sum(int(f.get('candidate_rows') or 0) for f in feeds)
    inflight_h = (round(inflight_rows * median_h / ceiling, 2)
                  if median_h and ceiling > 0 else 0.0)
    committed_h = (round((runnable_h or 0.0) + inflight_h, 2)
                   if runnable_h is not None else None)
    canary = gates.get('canary') or {}
    credential = gates.get('credential') or {}
    fast = fast_detectors(q.get('gate'))
    eligible = [e for e in backlog if not e['problems']]
    h_low, h_high = float(qs['h_low']), float(qs['h_high'])
    j_max, t_max = int(qs['j_max'] or 0), int(qs['t_max'] or 0)

    base = {
        'queue': queue, 'mode': mode,
        'not_started': not_started, 'running': running, 'ceiling': ceiling,
        'tasks_active': tasks_active,
        'median_walltime_h': median_h, 'p90_start_latency_h': p90_start_h,
        'runnable_h': runnable_h,
        'inflight_feeds': len(feeds), 'inflight_h': inflight_h,
        'committed_h': committed_h,
        'h_low': h_low, 'h_high': h_high, 'j_max': j_max, 't_max': t_max,
        'feed_switch': bool(qs['feed']), 'breaker': qs['breaker'],
        'canary': canary.get('status', ''), 'canary_age_h': canary.get('age_h'),
        'credential': credential.get('outcome', ''),
        'credential_age_h': credential.get('age_h'),
        'gate_finished': fast['finished'], 'gate_failed': fast['failed'],
        'gate_fast_failed': fast['fast_failed'],
        'ready_total': len(backlog), 'ready_eligible': len(eligible),
        'last_feed_age_h': last_feed_age_h,
    }

    def rec(candidate=None, **more):
        r = dict(base)
        r['candidate'] = candidate['task'] if candidate else ''
        r['candidate_rows'] = candidate['rows'] if candidate else None
        r['candidate_level'] = candidate['level'] if candidate else None
        r.update(more)
        return r

    if not enabled:
        return [('held', 'front_off', rec())]
    if not qs['feed']:
        return [('held', 'queue_off', rec())]
    if str(qs['breaker']) != 'closed':
        return [('degraded', f"breaker_{qs['breaker']}", rec())]
    if credential.get('red'):
        return [('degraded', 'credential', rec(gate_reason=credential.get('reason', '')))]
    if canary.get('red'):
        return [('degraded', 'canary', rec(gate_reason=canary.get('reason', '')))]
    if fast['red']:
        return [('degraded', fast['code'], rec(gate_reason=fast['reason']))]
    if feeds:
        return [('awaiting_observation', 'awaiting_observation', rec())]
    if committed_h is None:
        return [('held', 'no_calibration', rec())]
    if committed_h >= h_low:
        return [('supplied', 'supplied', rec())]
    if (ceiling > 0 and not_started > 0 and running < IDLE_FRACTION * ceiling
            and p90_start_h is not None and last_feed_age_h is not None
            and last_feed_age_h > p90_start_h):
        return [('idle_capacity', 'not_pulling', rec())]
    if not eligible:
        return [('no_work', 'no_eligible_task', rec())]

    out = []
    committed, fed_rows, fed = committed_h, inflight_rows, 0
    for candidate in eligible:
        if fed >= max_per_cycle or committed >= h_low:
            break
        rows = candidate['rows']
        if not rows:
            out.append(('held', 'unsized_task', rec(candidate)))
            break
        candidate_h = round(rows * median_h / ceiling, 2)
        if candidate_h > h_high:
            out.append(('oversize', 'oversize_task', rec(candidate, candidate_h=candidate_h)))
            break
        if committed + candidate_h > h_high:
            out.append(('held', 'would_exceed_high',
                        rec(candidate, candidate_h=candidate_h,
                            committed_after=round(committed + candidate_h, 2))))
            break
        if t_max and tasks_active + fed + 1 > t_max:
            out.append(('held', 'task_cap', rec(candidate, candidate_h=candidate_h)))
            break
        if j_max and not_started + fed_rows + rows > j_max:
            out.append(('held', 'job_cap', rec(candidate, candidate_h=candidate_h)))
            break
        state = 'fed' if mode == 'active' else 'would_feed'
        committed = round(committed + candidate_h, 2)
        fed_rows += rows
        fed += 1
        out.append((state, f"{state}:{candidate['task']}",
                    rec(candidate, candidate_h=candidate_h, committed_after=committed)))
    if not out:
        out.append(('supplied', 'supplied', rec()))
    return out


# The cycle

def run_cycle(*, dry_run=False, created_by='front'):
    """One cycle over the regulated queues. Returns the per-queue
    decisions and the cycle summary; writes the records and the stored
    state unless dry_run."""
    from monitor_app.epicprod_logging import log_epicprod_action
    from monitor_app.panda.census import queue_census

    t0 = timezone.now()
    errors = []

    def failed(label):
        def _f(exc):
            errors.append(f'{label}: {type(exc).__name__}: {exc}')
            return None
        return _f

    enabled = bool(setting('front.enabled', False))
    mode_requested = str(setting('front.mode', 'shadow'))
    mode = mode_requested
    if mode not in MODES:
        logger.error('front.mode %r is not available (the feed is not built); deciding as shadow',
                     mode_requested)
        errors.append(f'front.mode {mode_requested!r} is not available; decided as shadow')
        mode = 'shadow'
    queues = regulated_queues()
    max_per_cycle = max(1, int(setting('front.max_per_cycle', 2) or 1))
    activation_window_s = int(setting('front.activation_window_s', 600))

    census = _safe('census', queue_census, failed('census'))
    backlog = _safe('backlog', ready_backlog, failed('backlog'))
    if backlog is None:
        backlog = {}
    credential = _safe('credential gate', _credential_gate, lambda exc: {
        'outcome': 'unread', 'age_h': None, 'red': True,
        'reason': f'credential check unreadable: {exc}'})

    decisions, written = [], 0
    for queue in queues:
        settings = _safe(f'{queue} settings', lambda: queue_settings(queue),
                         lambda exc: dict(QUEUE_DEFAULTS))
        canary = _safe(f'{queue} canary gate', lambda: _canary_gate(queue), lambda exc: {
            'status': 'unread', 'age_h': None, 'red': True,
            'reason': f'canary unreadable: {exc}'})
        feeds = _safe(f'{queue} feeds', lambda: _recent_feeds(queue, activation_window_s), [])
        last_feed_age_h = _safe(f'{queue} last feed', lambda: _last_feed_age_h(queue), None)
        census_q = (census or {}).get('queues', {}).get(queue) if census else None
        if census is None:
            outcomes = [('held', 'census_unavailable',
                         {'queue': queue, 'mode': mode, 'ready_total': len(backlog.get(queue, [])),
                          'ready_eligible': 0, 'h_low': settings['h_low'], 'h_high': settings['h_high']})]
        else:
            outcomes = _safe(
                f'{queue} decision',
                lambda: decide(queue, census_q, backlog.get(queue, []), settings,
                               {'canary': canary, 'credential': credential}, feeds,
                               enabled=enabled, mode=mode, max_per_cycle=max_per_cycle,
                               last_feed_age_h=last_feed_age_h),
                lambda exc: [('error', 'decision_error',
                              {'queue': queue, 'mode': mode, 'error': f'{type(exc).__name__}: {exc}',
                               'h_low': settings['h_low'], 'h_high': settings['h_high'],
                               'ready_total': len(backlog.get(queue, [])), 'ready_eligible': 0})])
        last = _safe(f'{queue} last decision', lambda: _last_decision(queue), None)
        last_extra = (last or {}).get('extra_data') or {}
        stale = (last is not None and (t0 - last['timestamp']).total_seconds() >= HEARTBEAT_S)
        for state, reason, record in outcomes:
            record.update(state=state, reason=reason,
                          observed_at=(census or {}).get('observed_at') if census else None)
            is_feed = state in FEED_STATES
            changed = (last is None or last_extra.get('reason') != reason
                       or last_extra.get('state') != state)
            recorded = bool(is_feed or changed or stale or state == 'error')
            # The record the page links to: this cycle's when written,
            # else the standing one this decision repeats.
            shown = dict(record, recorded=recorded,
                         log_id=None if (recorded and not dry_run) else (last or {}).get('id'))
            decisions.append(shown)
            if dry_run or not recorded:
                continue
            depth = (f"{record.get('committed_h')} h" if record.get('committed_h') is not None
                     else 'no calibration')
            shown['log_id'] = _safe(f'{queue} record', lambda: log_epicprod_action(
                'front', 'front_decision', subject_type='panda_queue',
                subject_key=queue, username=created_by, outcome=state,
                sublevel='normal' if (is_feed or state == 'error') else 'low',
                live_default=is_feed,
                message=(f'front {queue}: {state} ({reason}); committed {depth} '
                         f'of {record.get("h_low")}-{record.get("h_high")} h; '
                         f'{record.get("ready_eligible", 0)} eligible of '
                         f'{record.get("ready_total", 0)} ready'),
                **{k: v for k, v in record.items() if k != 'queue'}), failed(f'{queue} record'))
            written += 1

    latest = {}
    for d in decisions:
        latest[d['queue']] = d
    summary = {'observed_at': (census or {}).get('observed_at') if census else None,
               'mode': mode, 'mode_requested': mode_requested, 'enabled': enabled,
               'queues': len(queues), 'decisions_recorded': written,
               'errors': errors,
               'states': {q: d['state'] for q, d in latest.items()},
               'duration_s': round((timezone.now() - t0).total_seconds(), 1)}
    if not dry_run:
        state_payload = {'observed_at': summary['observed_at'], 'cycle_at': t0.isoformat(),
                         'mode': mode, 'mode_requested': mode_requested, 'enabled': enabled,
                         'queues': latest, 'backlog': backlog, 'errors': errors,
                         'duration_s': summary['duration_s']}
        from monitor_app.cached_product import get_product
        _safe('state store', lambda: get_product(
            STATE_KEY, lambda: state_payload, ttl_seconds=STATE_TTL_S, refresh=True),
            failed('state store'))
        _safe('cycle record', lambda: log_epicprod_action(
            'front', 'front_cycle', username=created_by,
            outcome='error' if errors else 'ok',
            duration_ms=int(summary['duration_s'] * 1000),
            sublevel='normal' if errors else 'low', live_default=bool(errors),
            level=logging.ERROR if errors else logging.INFO,
            message=(f'front cycle ({mode}): '
                     + ', '.join(f'{q} {s}' for q, s in summary['states'].items())
                     + (f'; errors: {"; ".join(errors)}' if errors else '')),
            **{k: v for k, v in summary.items() if k not in ('states', 'errors')}),
            failed('cycle record'))
    return decisions, summary
