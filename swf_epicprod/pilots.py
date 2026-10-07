"""Pilot-side regulation (docs/CONTINUOUS_PRODUCTION.md, Pilot-side
regulation: the harvester's queue limits): for each regulated harvester
queue, the limits the harvester's queue configuration should carry,
decided from the harvester reporter's record and the front's census
against a running target.

The decision runs inside the front's cycle (``front.run_cycle``) and is
recorded as one ``pilot_decision`` per queue per cycle, and in the
stored front state under ``pilots``, which the front page reads.
Shadow mode only: the cycle decides and records the limits it would
set and changes nothing on the harvester. Active mode, with the
credentialed doer that edits the queue's entry, follows the shadow
record's comparison with the realized running count.

Settings in SysConfig, seeded at their defaults on first read:

- ``pilot.mode`` ('shadow'): the only mode accepted until the doer is
  built; any other value is recorded as an error and decided as shadow.
- ``pilot.queues`` (['UM_GREX_PanDA_1']): the regulated queues.
- ``pilot.queue.<queue>.target_running`` (0 = unset): the running
  target, set by the operator from the site's allocation.
- ``pilot.queue.<queue>.enabled`` (False): the per-queue switch for
  active mode.

The limits, per queue (harvester keys in the queue's entry of
``panda_queueconfig.json``): ``maxWorkers`` = T + Q;
``nQueueLimitWorkerMax`` = Q; ``nQueueLimitJobMax`` =
``nQueueLimitJobMin`` = Q plus one cycle's new pilots, ratio unset;
``maxNewWorkersPerCycle`` = enough to refill Q in ``REFILL_CYCLES``
cycles. T is the running target; Q, the queued-pilot target, is one
hour of pilot starts at the measured rate, between ``QUEUED_FLOOR``
and T.
"""
import logging
import math

logger = logging.getLogger(__name__)

DEFAULT_QUEUES = ['UM_GREX_PanDA_1']
QUEUE_DEFAULTS = {'enabled': False, 'target_running': 0}
MODES = ('shadow',)
HARVESTER_HOSTS = ('pandaharvester01', 'pandaharvester02', 'osgsub01-harvester')
QUEUED_HORIZON_H = 1.0
QUEUED_FLOOR = 50
REFILL_CYCLES = 10
STEP_UP = 0.5
REPORT_STALE_INTERVALS = 3
DEGRADED_MIN_ENDED = 20
DEGRADED_FRACTION = 0.5
SATURATED_HOLD_H = 1.0
# The harvester keys the regulator sets, and the pq_table reading of
# each where the harvester records it.
LIMIT_KEYS = ('maxWorkers', 'nQueueLimitWorkerMax', 'nQueueLimitJobMax',
              'nQueueLimitJobMin', 'maxNewWorkersPerCycle')
IN_FORCE_FIELDS = {'maxWorkers': 'max_workers',
                   'nQueueLimitWorkerMax': 'n_queue_limit_worker',
                   'nQueueLimitJobMax': 'n_queue_limit_job'}


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def harvester_reading(queue, reports):
    """The queue's pilot supply as its harvester host reports it
    (swf-monitor docs/HARVESTER_REPORTER.md), from ``reports``
    {host: latest report}. None when no reporting harvester serves the
    queue."""
    for host in HARVESTER_HOSTS:
        report = reports.get(host) or {}
        record = report.get('record') or {}
        db = record.get('harvester_db') or {}
        limits = next((q for q in (db.get('launch_limits') or [])
                       if isinstance(q, dict) and q.get('queue') == queue), None)
        if limits is None:
            continue
        now = (db.get('workers_now') or {}).get(queue) or {}
        ended = ((db.get('workers_ended') or {}).get('sites') or {}).get(queue) or {}
        by_status = ended.get('by_status') or {}
        n_ended = _int(ended.get('ended')) or 0
        return {
            'host': host,
            'report_age_s': report.get('age_seconds'),
            'interval_s': _int((record.get('interval') or {}).get('seconds')),
            'running': _int(now.get('running')) or 0,
            'queued': (_int(now.get('submitted')) or 0) + (_int(now.get('ready')) or 0),
            'ended': n_ended,
            'ended_unfinished': n_ended - (_int(by_status.get('finished')) or 0),
            'in_force': {key: _int(limits.get(field))
                         for key, field in IN_FORCE_FIELDS.items()},
            'db_error': db.get('error'),
        }
    return None


def target_limits(target, queued_target):
    """The harvester limits for running target T and queued target Q."""
    new = max(1, math.ceil(queued_target / REFILL_CYCLES))
    return {'maxWorkers': target + queued_target,
            'nQueueLimitWorkerMax': queued_target,
            'nQueueLimitJobMax': queued_target + new,
            'nQueueLimitJobMin': queued_target + new,
            'maxNewWorkersPerCycle': new}


def floor_limits():
    return target_limits(0, QUEUED_FLOOR) | {'maxWorkers': QUEUED_FLOOR}


def _bounded(desired, in_force, *, raise_allowed):
    """Each limit moved toward its desired value: lowered at once, raised
    by at most ``STEP_UP`` of the value in force, not raised at all when
    raising is held. A limit with no reading in force moves freely."""
    out = {}
    for key, want in desired.items():
        have = in_force.get(key)
        if key == 'nQueueLimitJobMin':
            have = in_force.get('nQueueLimitJobMax')
        if have is None or want <= have:
            out[key] = want
        elif not raise_allowed:
            out[key] = have
        else:
            out[key] = min(want, max(int(have * (1 + STEP_UP)), have + QUEUED_FLOOR))
    return out


def decide(queue, reading, census_q, blockers, settings, *, mode, now, last=None):
    """The queue's pilot decision: (state, reason, record). Pure.

    ``reading`` is ``harvester_reading``'s; ``census_q`` the front's
    census of the queue; ``blockers`` the front's gate reasons for the
    queue, without the credential gate (pilots carry no operator
    credential); ``last`` the previous cycle's record for the queue;
    ``now`` an aware datetime."""
    target = _int(settings.get('target_running')) or 0
    record = {'queue': queue, 'mode': mode, 'target_running': target,
              'decided_at': now.isoformat()}
    if reading is None:
        return 'held', 'no_report', record
    record.update({k: reading[k] for k in ('host', 'report_age_s', 'interval_s', 'running',
                                           'queued', 'ended', 'ended_unfinished', 'in_force')})
    interval_s = reading.get('interval_s')
    age_s = reading.get('report_age_s')
    if reading.get('db_error') or not interval_s:
        record['error'] = reading.get('db_error') or 'report has no interval'
        return 'held', 'report_unreadable', record
    if age_s is None or age_s > REPORT_STALE_INTERVALS * interval_s:
        return 'held', 'report_stale', record
    if target <= 0:
        return 'held', 'no_target', record

    census_q = census_q or {}
    activated = int(((census_q.get('by_status') or {}).get('activated') or {}).get('jobs') or 0)
    p90_h = (census_q.get('calibration') or {}).get('p90_start_latency_h')
    # Pilots started per hour, read as pilots ended: the two balance while
    # the running count holds.
    start_rate_h = round(reading['ended'] * 3600.0 / interval_s, 1)
    queued_target = int(min(max(round(start_rate_h * QUEUED_HORIZON_H), QUEUED_FLOOR), target))
    record.update({'activated': activated, 'p90_start_h': p90_h,
                   'start_rate_h': start_rate_h, 'queued_target': queued_target})
    in_force = reading['in_force']

    ended, unfinished = reading['ended'], reading['ended_unfinished']
    failing = ended >= DEGRADED_MIN_ENDED and unfinished >= DEGRADED_FRACTION * ended
    if blockers or failing:
        record['gate_reason'] = '; '.join(blockers) if blockers else (
            f'{unfinished} of {ended} pilots ended unfinished in the interval')
        record['would_set'] = floor_limits()
        return _settled(record, in_force, mode, 'degraded')

    desired = target_limits(target, queued_target)
    # The site not starting what is queued: the queue held at Q while
    # running stays below T, for longer than the start latency.
    saturated = reading['queued'] >= queued_target and reading['running'] < target
    since = None
    if saturated:
        since = ((last or {}).get('saturated_since') if (last or {}).get('saturated') else None) \
            or now.isoformat()
    record.update({'saturated': saturated, 'saturated_since': since})
    # A fixed hour: the census's p90 start latency is the PanDA job's
    # wait, which a backlog stretches to days (49 h at GREX, 10/7), not
    # the pilot's wait at the site.
    reason = None
    if saturated and _hours_since(since, now) >= SATURATED_HOLD_H:
        reason = 'site_not_starting'
    elif activated < queued_target:
        reason = 'no_work'
    record['would_set'] = _bounded(desired, in_force, raise_allowed=reason is None)
    return _settled(record, in_force, mode, reason)


def _hours_since(iso, now):
    from datetime import datetime
    try:
        return (now - datetime.fromisoformat(iso)).total_seconds() / 3600.0
    except (TypeError, ValueError):
        return 0.0


def _settled(record, in_force, mode, reason):
    """State from the limits to set against those in force."""
    would = record['would_set']
    # Compared where the harvester records the value in force; the
    # job minimum and the per-cycle rate are not in pq_table.
    change = {k: (in_force.get(k), would[k]) for k in IN_FORCE_FIELDS
              if in_force.get(k) != would[k]}
    record['change'] = change
    if reason == 'degraded':
        return ('would_set' if change else 'degraded'), 'degraded', record
    if change:
        return 'would_set', reason or 'regulate', record
    return 'steady', reason or 'in_force', record


def setting(key, default):
    from monitor_app.models import SysConfig
    return SysConfig.get_setting(key, default)


def queue_settings(queue):
    return {k: setting(f'pilot.queue.{queue}.{k}', v) for k, v in QUEUE_DEFAULTS.items()}


def regulated_queues():
    value = setting('pilot.queues', DEFAULT_QUEUES)
    if isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
        return [v for v in value if v]
    logger.error('pilot.queues is not a list of queue names: %r; using the defaults', value)
    return list(DEFAULT_QUEUES)


def run(census, placement, previous, *, now, errors):
    """The cycle's pilot decisions: {queue: record}, each with its state
    and reason. ``placement`` is the front cycle's per-queue gates;
    ``previous`` the stored state's ``pilots``; failures are appended to
    ``errors`` and decided as held."""
    from monitor_app.host_reports import latest
    from swf_epicprod.front import gate_blockers

    mode = str(setting('pilot.mode', 'shadow'))
    if mode not in MODES:
        errors.append(f'pilot.mode {mode!r} is not one of {MODES}; decided as shadow')
        mode = 'shadow'
    reports = {}
    for host in HARVESTER_HOSTS:
        try:
            reports[host] = latest(host) or {}
        except Exception as exc:  # noqa: BLE001
            logger.exception('pilots: report %s unreadable', host)
            errors.append(f'pilot report {host}: {type(exc).__name__}: {exc}')
    out = {}
    for queue in regulated_queues():
        try:
            gates = dict((placement.get(queue) or {}).get('gates') or {})
            gates.pop('credential', None)
            blockers = gate_blockers(gates) if gates else []
            census_q = ((census or {}).get('queues') or {}).get(queue)
            state, reason, record = decide(
                queue, harvester_reading(queue, reports), census_q, blockers,
                queue_settings(queue), mode=mode, now=now,
                last=(previous or {}).get(queue))
        except Exception as exc:  # noqa: BLE001
            logger.exception('pilots: %s decision failed', queue)
            errors.append(f'pilot {queue}: {type(exc).__name__}: {exc}')
            state, reason, record = 'error', 'decision_error', {
                'queue': queue, 'mode': mode, 'error': f'{type(exc).__name__}: {exc}'}
        record.update(state=state, reason=reason)
        out[queue] = record
    return out
