"""The pressure front's decision cycle (docs/CONTINUOUS_PRODUCTION.md,
The dispatcher): for each regulated queue, read the census, the gates
and the ordered ``ready`` backlog, decide feed or hold with a reason,
and record the decision on the action stream.

The cycle runs as the production-operations agent's ``front_cycle``
doer (swf-monitor ``scripts/front-cycle.py``), every five minutes by
cron enqueue. It holds no credential: a feed is the existing PCS submit
request (``pcs.services.prodtask_submit_request``, the compose panel's
Submit), which allocates the attempt and enqueues the credentialed
submitter. In shadow mode the cycle decides and records but never
submits; the decision it would have made reads ``would_feed``. In
active mode a ``fed`` decision submits; a refused feed is recorded as
``error (feed_failed)`` and decided again next cycle.

Settings live in SysConfig under ``front.*`` and are seeded at their
defaults on first read, so every knob is visible on the System page:

- ``front.enabled`` (False): the global switch.
- ``front.mode`` ('shadow'): shadow, or active.
- ``front.jedi_throttled`` (False): phase two, once the ePIC job
  throttler paces generation in JEDI: committed depth counts the
  queue's ungenerated rows, and the job cap and oversize hold retire.
- The regulated queues and the closed ones are the queue registry's
  (``swf_epicprod.queues``, docs/QUEUE_REGISTRY.md), not ``front.*``.
- ``front.max_per_cycle`` (2): feeds per queue per cycle.
- ``front.activation_window_s`` (600): a feed counts as committed depth
  for this long before the census is expected to show it.
- ``front.queue.<queue>.feed`` (False), ``.h_low`` (8), ``.h_high``
  (24), ``.j_max`` (0 = none), ``.t_max`` (0 = none), ``.breaker`` ('closed'),
  ``.feed_exempt`` (''): with the feed off, the reason the queue is fed
  by its own feeder rather than the front (``scripts/front-feed.py``);
  ``.nodes`` (1): for a whole-node queue, the nodes its work is to fill,
  set with the harvester's worker ceiling (``decide``).
- ``front.idle_move.mode`` ('active'), ``.max_per_cycle`` (4),
  ``.cooldown_h`` (12): the idle-queue move (``swf_epicprod.idle_move``).

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
from datetime import datetime, timedelta

from django.core.cache import cache
from django.utils import timezone
from swf_epicprod import queues as registry
from swf_epicprod.regulation import CAPACITY_POOLS, RETIRED_QUEUES, throttler_verdict

logger = logging.getLogger(__name__)

QUEUE_DEFAULTS = {'feed': False, 'h_low': 8.0, 'h_high': 24.0,
                  'j_max': 0, 't_max': 0, 'breaker': 'closed', 'feed_exempt': '',
                  'nodes': 1}
MODES = ('shadow', 'active')
FEED_SOURCE = 'pcs_front_feed'
HEARTBEAT_S = 3600
CANARY_STALE_H = 24
CREDENTIAL_STALE_H = 36
# The fast detectors, until the alarm-queue modules exist.
BURN_MIN_FAILED = 10
BURN_FAST_FRACTION = 0.5
FAILURE_WINDOW_MIN_OUTCOMES = 50
FAILURE_WINDOW_RATE = 0.6
ROWS_CACHE_TTL_S = 24 * 3600
STATE_KEY = 'front_state'
STATE_TTL_S = 24 * 3600
STALL_GRACE_H = 0.25
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


def _config():
    from monitor_app.models import SysConfig
    return SysConfig.get_config()


def queue_settings(queue):
    out = {}
    for key, default in QUEUE_DEFAULTS.items():
        out[key] = setting(f'front.queue.{queue}.{key}', default)
    return out


def regulated_queues():
    """The queues the front regulates, in the queue registry's order
    (docs/QUEUE_REGISTRY.md); a retired queue never among them."""
    return registry.regulated()


def closed_queues():
    """{queue: reason} for the queues closed to new work in the queue
    registry: off, by an operator or by the NERSC balance
    (``swf_epicprod.nersc_allocation``), or retired. A closed queue is never
    recommended, and PCS refuses to place, submit, rerun, trial or move work
    there. Canary probes do not pass through PCS submission and still run."""
    return registry.closed()


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


def _declared_gate(queue, horizon_h):
    """What CRIC declares for the queue (swf-monitor monitor_app/declared.py;
    CONTINUOUS_PRODUCTION.md, Declared downtime): red with a rule in
    force or a window starting within ``horizon_h`` hours, the committed
    depth the front would otherwise fill. A hold, not a fault: the
    breaker never opens on it, and it lifts itself when the window ends."""
    from monitor_app.declared import gate_for_queue
    return gate_for_queue(queue, horizon_h)


def credential_verdict(outcome, days_left, age_h):
    """(red, reason) for the nightly credential check's record. Pure.

    Red when the check is older than CREDENTIAL_STALE_H, or when a
    credential is expired, missing or cannot be vouched for. A warning
    whose every credential still has days left is a credential inside its
    warning window: it still works, the ping the credential proposer
    entered carries the date, and holding production for the week before
    the expiry would stop it for nothing (the eicprod proxy's window
    opened on 2026-10-22, a week before its 10-28 expiry)."""
    if age_h > CREDENTIAL_STALE_H:
        return True, f'credential check {age_h} h old'
    if outcome == 'ok':
        return False, ''
    if (outcome == 'warning' and isinstance(days_left, dict) and days_left
            and all(isinstance(d, (int, float)) and d > 0 for d in days_left.values())):
        return False, ''
    return True, f'credential check {outcome or "unrecorded"}'


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
    extra = row['extra_data'] or {}
    outcome = str(extra.get('outcome') or '')
    red, reason = credential_verdict(outcome, extra.get('days_left'), age_h)
    return {'outcome': outcome, 'age_h': age_h, 'red': red, 'reason': reason}


def gate_waiver(queue, now=None):
    """The queue's active entry in ``front.gate_waivers`` ({queue: {until,
    reason, by}}, until an ISO time), or None. An operator's time-limited
    word that the fast detectors' reading of this queue is known and not
    the queue's health; it waives those detectors alone."""
    value = setting('front.gate_waivers', {})
    if not isinstance(value, dict):
        logger.error('front.gate_waivers is not a {queue: {until, reason, by}} mapping: %r', value)
        return None
    w = value.get(queue)
    if not isinstance(w, dict) or not w.get('until'):
        return None
    try:
        until = datetime.fromisoformat(str(w['until']))
    except ValueError:
        logger.error('front.gate_waivers[%s].until is not an ISO time: %r', queue, w.get('until'))
        return None
    if until.tzinfo is None:
        until = timezone.make_aware(until)
    if until <= (now or timezone.now()):
        return None
    return {'until': until.isoformat(), 'reason': str(w.get('reason') or ''),
            'by': str(w.get('by') or '')}


def fast_detectors(gate, waiver=None):
    """Burn-through and the windowed failure rate from the census gate
    window (CONTINUOUS_PRODUCTION.md, The tripwire). Failures of tasks a
    finding explains are counted apart by the census and are not among
    the failed. ``waiver`` (gate_waiver) turns a red reading green with
    the waiver stated beside it. Pure."""
    g = gate or {}
    finished, failed, fast = (int(g.get('finished') or 0), int(g.get('failed') or 0),
                              int(g.get('fast_failed') or 0))
    out = {'finished': finished, 'failed': failed, 'fast_failed': fast,
           'explained': int(g.get('explained') or 0),
           'explained_by': list(g.get('explained_by') or []),
           'red': False, 'reason': '', 'code': ''}
    if failed >= BURN_MIN_FAILED and fast / max(failed, 1) >= BURN_FAST_FRACTION and finished == 0:
        out.update(red=True, code='burn_through',
                   reason=f'burn-through: {fast} of {failed} failures fast, none finished')
    else:
        outcomes = finished + failed
        if outcomes >= FAILURE_WINDOW_MIN_OUTCOMES and failed / outcomes >= FAILURE_WINDOW_RATE:
            out.update(red=True, code='failure_window',
                       reason=f'failure window: {failed} of {outcomes} outcomes failed')
    if out['red'] and waiver:
        out.update(red=False, waived=out['code'],
                   reason=(f"waived until {waiver['until']}"
                           + (f" by {waiver['by']}" if waiver.get('by') else '')
                           + (f": {waiver['reason']}" if waiver.get('reason') else '')
                           + f" ({out['reason']})"),
                   code='waived')
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
    never fed it, for the queue's decision record and display."""
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
    from pcs.commands import prodtask_manifest_rows
    key = _rows_key(task, cfg)
    if key is None:
        return None
    cached = cache.get(key)
    if cached is not None:
        return cached
    rows = prodtask_manifest_rows(task, cfg=cfg)
    if rows is not None:
        cache.set(key, rows, ROWS_CACHE_TTL_S)
    return rows


def task_rows_cached(task, cfg):
    """``task_rows`` when the cycle has counted it, else None: a read for
    a page, which never triggers the catalog read."""
    key = _rows_key(task, cfg)
    return cache.get(key) if key else None


def _rows_key(task, cfg):
    from pcs.commands import prodtask_events_per_job
    n_events = prodtask_events_per_job(task, cfg=cfg)
    if n_events <= 0:
        return None
    dids = sorted(str((i or {}).get('did') or '') for i in (task.inputs or []))
    digest = hashlib.sha1('|'.join(dids).encode()).hexdigest()[:12]
    return f'front:rows:{task.pk}:{n_events}:{digest}'


def open_attempt_problem(task):
    """The readiness problem of a task whose submission is in flight or
    orphaned: an allocated ``PandaTasks`` attempt without a jediTaskID
    that is not marked ``submit_failed``. The front never submits such a
    task again; an orphan is the operator's to record."""
    row = (task.panda_tasks.filter(jedi_task_id__isnull=True)
           .exclude(status_snapshot='submit_failed')
           .order_by('-try_number').first())
    if row is None:
        return None
    return (f'a submission attempt is open: try {row.try_number} allocated '
            f'{row.created_at:%m/%d %H:%M} ({row.association_source or "unknown source"}), '
            f'no jediTaskID yet')


STORAGE_ALARM = 'alarm_storage_headroom'
# An event the alarm engine has not seen for this long is not current.
STORAGE_EVENT_FRESH_S = 3600
DEFAULT_OUT_RSE = 'EIC-XRD'  # the payload's default (run.sh OUT_RSE)


def full_output_rses(now=None):
    """{rse: subject} for every RSE the storage headroom alarm currently
    reads as near full, by door space or by catalog quota. Feeding work
    whose output lands there only loses the output when it fills
    (2026-10-02, BNL-XRD). Pure read of the alarm's active events; an
    alarm that is not running holds nothing."""
    from monitor_app.models import Entry
    alarm = (Entry.objects.filter(context_id='swf-alarms', kind='alarm',
                                  data__entry_id=STORAGE_ALARM)
             .values_list('id', flat=True).first())
    if alarm is None:
        return {}
    now = (now or timezone.now()).timestamp()
    out = {}
    events = (Entry.objects.filter(context_id='swf-alarms', kind='event',
                                   data__alarm_config_id=str(alarm),
                                   data__clear_time=None)
              .values_list('data', flat=True))
    for data in events:
        rse = str(data.get('rse') or '')
        if rse and now - float(data.get('last_seen') or 0) < STORAGE_EVENT_FRESH_S:
            out.setdefault(rse, str(data.get('subject') or 'near full'))
    return out


def output_storage_problem(cfg, full):
    """The readiness problem of a task whose output RSE is near full."""
    rse = str(cfg.get('rucio_rse') or DEFAULT_OUT_RSE)
    if rse in full:
        return f'its output storage {rse} is near full ({full[rse]}); free space or move its output'
    return None


def ready_backlog():
    """The ``ready`` tasks by pinned queue, eligible ones first in
    priority order (level 1 first, unset last, then oldest), each with
    its declared rows and readiness problems. A task whose reading
    fails is carried with the failure as its problem."""
    from pcs.commands import pinned_site, prodtask_priority_level
    from pcs.models import ProdTask
    from pcs.services import prodtask_readiness_problems
    from django.db.models import Q
    by_queue = {}
    full = _safe('reading output storage', full_output_rses, {})
    # The ready tasks, every task a person queued for the front
    # (``overrides['queued']``: a submit or a rerun), whatever its status,
    # and every task a person unplaced in PanDA (``overrides['unplaced']``).
    tasks = (ProdTask.objects.filter(Q(status='ready') | Q(overrides__has_key='queued')
                                     | Q(overrides__has_key='unplaced'))
             .select_related('dataset', 'request', 'prod_config', 'campaign')
             .order_by('created_at'))
    for task in tasks:
        queued = (task.overrides or {}).get('queued') or None
        unplaced = (task.overrides or {}).get('unplaced') or None
        entry = {'task': task.composed_name, 'pk': task.pk, 'level': None,
                 'level_source': 'none', 'rows': None, 'problems': [],
                 'created_at': ((unplaced or queued or {}).get('at')
                                or task.created_at.isoformat()),
                 'kind': ('unplaced_move' if unplaced else
                          (queued or {}).get('kind') or 'submit'),
                 # Queued by a person: fed even where the feed switch is off.
                 'hand': bool(queued and queued.get('by')) and not unplaced}
        queue = 'unknown'
        try:
            cfg = task.get_effective_config()
            if unplaced:
                # In PanDA and unplaced: in the pool until a queue takes it
                # (CONTINUOUS_PRODUCTION.md, Unplace).
                from pcs.services import unplace_blocker
                queue = UNPLACED
                entry['level'], entry['level_source'] = prodtask_priority_level(task)
                code, problem = unplace_blocker(task, cfg, in_pool=True)
                entry['problems'] = [problem] if problem else []
                entry['unplace_lapsed'] = code in UNPLACE_LAPSING
                entry['unplaced'] = unplaced
                entry['rows'] = task_rows(task, cfg)
                # Its work in PanDA keeps its shape, so Event Service work
                # is refused by every queue other than a whole-node one.
                entry['need'] = task_need(cfg, rows=entry['rows'], event_service=False,
                                          es_bound=event_service_bound(task),
                                          es_site=event_service_site(task))
                by_queue.setdefault(queue, []).append(entry)
                continue
            queue = pinned_site(task, cfg=cfg) or UNPLACED
            entry['level'], entry['level_source'] = prodtask_priority_level(task)
            entry['problems'] = list(prodtask_readiness_problems(task))
            open_attempt = open_attempt_problem(task)
            if open_attempt:
                entry['problems'].append(open_attempt)
            storage = output_storage_problem(cfg, full)
            if storage:
                entry['problems'].append(storage)
            # Rows are read for every ready task that can be sized, so
            # the ready hours count the whole backlog, not only the
            # eligible part; a task without a matched input has none.
            # A queued residual carries the jobs measured when it was queued.
            entry['rows'] = (int(queued['rows']) if entry['kind'] == 'rerun_residual'
                             and (queued or {}).get('rows') else task_rows(task, cfg))
            # A whole-node queue (Perlmutter, Google) takes only what the Event
            # Service can run over EVGEN input (open_to, feed_task).
            from .evgen_es import capable
            entry['evgen_es'] = entry['kind'] == 'submit' and capable(task, cfg)
            entry['need'] = task_need(cfg, rows=entry['rows'], event_service=entry['evgen_es'],
                                      es_bound=event_service_bound(task),
                                      es_site=event_service_site(task))
            if queue != UNPLACED:
                from .queues import dispatch_capabilities, image_problem
                problem = image_problem(entry['need']['container_image'],
                                        dispatch_capabilities().get(queue))
                if problem:
                    entry['problems'].append(problem)
        except Exception as exc:  # noqa: BLE001
            logger.exception('front: reading ready task %s failed', task.pk)
            entry['problems'].append(f'reading the task failed: {type(exc).__name__}: {exc}')
        by_queue.setdefault(queue, []).append(entry)
    for entries in by_queue.values():
        entries.sort(key=ENTRY_ORDER)
    return by_queue


UNPLACED = 'unplaced'
# pcs.services.LAPSING_BLOCKERS: an unplaced task whose work started
# (or that no move can take) is no longer the front's to place.
UNPLACE_LAPSING = ('trial', 'status', 'started')


def lapse_unplaced(pool, *, created_by, dry_run):
    """Clear the mark of each unplaced task in ``pool`` whose unplace no
    longer applies (its work started where it is, or no move can take
    it), recorded; returns the pool without them."""
    from pcs.models import ProdTask
    from pcs.services import ServiceError, prodtask_place_unplaced
    kept = []
    for entry in pool:
        if not entry.get('unplace_lapsed'):
            kept.append(entry)
            continue
        if dry_run:
            continue
        try:
            prodtask_place_unplaced(task=ProdTask.objects.get(pk=entry['pk']), site='',
                                    changed_by=created_by)
        except ServiceError:
            pass        # refused as expected: the mark is cleared and recorded
    return kept


def _stored_decisions():
    """The previous cycle's per-queue decisions, from the stored state."""
    from monitor_app.models import CachedProduct
    row = CachedProduct.objects.filter(key=STATE_KEY).first()
    return ((row.value or {}).get('queues') or {}) if row else {}


def _stored_pilots():
    """The previous cycle's pilot decisions, from the stored state."""
    from monitor_app.models import CachedProduct
    row = CachedProduct.objects.filter(key=STATE_KEY).first()
    return ((row.value or {}).get('pilots') or {}) if row else {}


# The holds that mean a queue takes no new task this cycle (decide).
CAP_HOLDS = ('task_cap', 'job_cap')


ENTRY_ORDER = lambda e: (bool(e['problems']), e['level'] or 9, e['created_at'])  # noqa: E731


def draw_order(queues, feedable, state):
    """The order in which queues draw on the ready pool: the feedable
    queues as the recommendation ranks them with no task's limits (need,
    then depth, then start latency), then the rest. Ready tasks hold no
    queue (Torre Wenaus, 2026-10-28: "READY QUEUE TASKS ARE AGNOSTIC, BY
    DEFINITION"): a queue takes one only when it is below its low mark,
    and the neediest asks first."""
    rows = recommend(state, None, feedable)['rows'] if feedable else []
    ranked = [r['queue'] for r in sorted((r for r in rows if r.get('rank')),
                                         key=lambda r: r['rank'])]
    return ranked + [q for q in queues if q not in ranked]


def open_to(pool, queue, state):
    """The ready pool's entries ``queue`` may take: ready (no readiness
    problem) and open to it by the recommendation's own reading (feed on,
    no red gate, a fit to its limits and image). Each is returned as a
    copy carrying its placement on the queue, which rides to the feed; an
    entry the queue may not take keeps the reason as ``unplaced_reason``
    and the queues it is open to as ``open_to``. Mutates only those two
    fields of ``pool``'s entries."""
    out = []
    for entry in pool:
        if entry.get('problems'):
            entry['unplaced_reason'] = 'not ready'
            continue
        refusal = event_service_refusal(queue, entry.get('need'))
        if refusal:
            entry['unplaced_reason'] = refusal
            continue
        result = recommend(state, entry.get('need'), [queue])
        if result['recommended'] != queue:
            entry['unplaced_reason'] = result['reason']
            continue
        entry.setdefault('open_to', []).append(queue)
        out.append(dict(entry, placement={'recommended': queue, 'reason': result['reason']}))
    return out


def left_in_pool(pool, feedable):
    """The pool's entries no queue took this cycle, each with why. Pure
    apart from setting ``unplaced_reason``."""
    for entry in pool:
        if entry.get('problems'):
            entry['unplaced_reason'] = 'not ready'
        elif not feedable:
            entry['unplaced_reason'] = 'no queue the front feeds'
        elif entry.get('open_to'):
            # A task some queue may take is simply ready: a ready task names
            # no queue (Torre Wenaus, 2026-10-29), so no other queue's
            # refusal stands beside it either.
            entry.pop('unplaced_reason', None)
        entry.pop('open_to', None)
    return pool


def core_limits(cfg):
    """{queue: cores} for each queue under an active operator core limit
    (``pilot.queue.<queue>.worker_limit``, scripts/worker-limit.py)."""
    out = {}
    for key, control in (cfg or {}).items():
        if not (key.startswith('pilot.queue.') and key.endswith('.worker_limit')):
            continue
        if isinstance(control, dict) and control.get('active') and isinstance(
                control.get('core_limit'), int) and control['core_limit'] > 0:
            out[key[len('pilot.queue.'):-len('.worker_limit')]] = control['core_limit']
    return out


def core_limited(census, limits):
    """The census with each limited queue's ceiling at most its core limit
    and its hours at capacity scaled to match: a queue held to 296 cores
    is sized at 296, not at the 949 it once ran (2026-10-28, GREX). Pure;
    returns a new census."""
    if not census or not limits:
        return census
    queues = dict(census.get('queues') or {})
    for queue, limit in limits.items():
        q = queues.get(queue)
        ceiling = int((q or {}).get('ceiling') or 0)
        if not q or ceiling <= limit:
            continue
        q = dict(q, ceiling=limit, ceiling_observed=ceiling, ceiling_source='core limit')
        for key in ('hours_at_capacity', 'pool_runnable_h', 'pool_ungenerated_h'):
            if q.get(key) is not None:
                q[key] = round(q[key] * ceiling / limit, 2)
        queues[queue] = q
    return dict(census, queues=queues)


# The decision

def decide(queue, census_q, backlog, settings, gates, feeds, *, enabled, mode,
           max_per_cycle, last_feed_age_h, jedi_throttled=False):
    """One queue's decisions, pure: a list of ``(state, reason, record)``,
    one per feed, else one for the queue's state.

    ``census_q`` is the census's entry for the queue (or None), ``backlog``
    the queue's ready entries, ``settings`` the queue's ``front.queue.*``
    values, ``gates`` the canary and credential gates, ``feeds`` the
    front's feeds younger than the activation window, ``last_feed_age_h``
    the hours since its latest feed (None when it never fed the queue).
    ``jedi_throttled`` is phase two (CONTINUOUS_PRODUCTION.md, Two
    regulators): the ePIC job throttler bounds the activated pool, so
    committed depth counts the queue's ungenerated rows and the job cap
    and the oversize hold retire.
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
    ungenerated = int(q.get('ungenerated') or 0)
    runnable_h = q.get('pool_runnable_h', q.get('hours_at_capacity'))
    inflight_rows = sum(int(f.get('candidate_rows') or 0) for f in feeds)
    inflight_h = (round(inflight_rows * median_h / ceiling, 2)
                  if median_h and ceiling > 0 else 0.0)
    ungenerated_h = (round(ungenerated * median_h / ceiling, 2)
                     if median_h and ceiling > 0 else 0.0)
    if q.get('pool_ungenerated_h') is not None:
        ungenerated_h = q['pool_ungenerated_h']
    phase = 'throttled' if jedi_throttled else 'unthrottled'
    committed_h = (round((runnable_h or 0.0) + inflight_h
                         + (ungenerated_h if jedi_throttled else 0.0), 2)
                   if runnable_h is not None else None)
    canary = gates.get('canary') or {}
    credential = gates.get('credential') or {}
    declared = gates.get('declared') or {}
    regulator = gates.get('regulator') or {}
    fast = fast_detectors(q.get('gate'), settings.get('gate_waiver'))
    eligible = [e for e in backlog if not e['problems']]
    h_low, h_high = float(qs['h_low']), float(qs['h_high'])
    j_max, t_max = int(qs['j_max'] or 0), int(qs['t_max'] or 0)

    base = {
        'queue': queue, 'mode': mode, 'phase': phase,
        'not_started': not_started, 'running': running, 'ceiling': ceiling,
        'tasks_active': tasks_active,
        'ungenerated': ungenerated, 'ungenerated_h': ungenerated_h,
        'median_walltime_h': median_h, 'p90_start_latency_h': p90_start_h,
        'runnable_h': runnable_h,
        'inflight_feeds': len(feeds), 'inflight_h': inflight_h,
        'committed_h': committed_h,
        'h_low': h_low, 'h_high': h_high, 'j_max': j_max, 't_max': t_max,
        'feed_switch': bool(qs['feed']), 'breaker': qs['breaker'],
        'feed_exempt': bool(not qs['feed'] and qs.get('feed_exempt')),
        'canary': canary.get('status', ''), 'canary_age_h': canary.get('age_h'),
        'credential': credential.get('outcome', ''),
        'credential_age_h': credential.get('age_h'),
        'declared': declared.get('state', ''),
        'gate_finished': fast['finished'], 'gate_failed': fast['failed'],
        'gate_explained': fast['explained'], 'gate_fast_failed': fast['fast_failed'],
        'ready_total': len(backlog), 'ready_eligible': len(eligible),
        'last_feed_age_h': last_feed_age_h,
        'capacity_pool': q.get('capacity_pool'),
    }

    def rec(candidate=None, **more):
        r = dict(base)
        r['candidate'] = candidate['task'] if candidate else ''
        r['candidate_pk'] = candidate.get('pk') if candidate else None
        r['candidate_rows'] = candidate['rows'] if candidate else None
        r['candidate_level'] = candidate['level'] if candidate else None
        r['candidate_placement'] = candidate.get('placement') if candidate else None
        r['candidate_kind'] = candidate.get('kind', 'submit') if candidate else None
        r.update(more)
        return r

    if not enabled:
        return [('held', 'front_off', rec())]
    if not qs['feed']:
        # The switch governs the front's own placements; a task a person
        # queued here is still fed, under every other gate.
        backlog = [e for e in backlog if e.get('hand')]
        if not backlog:
            # Exempt: its own feeder supplies it, which is not a fault.
            return [('held', 'queue_exempt' if base['feed_exempt'] else 'queue_off', rec())]
        eligible = [e for e in backlog if not e['problems']]
        base['ready_total'], base['ready_eligible'] = len(backlog), len(eligible)
        base['hand_only'] = True
    if str(qs['breaker']) != 'closed':
        return [('degraded', f"breaker_{qs['breaker']}", rec())]
    if declared.get('red'):
        # A declared downtime in force, or one starting inside the
        # committed-depth horizon: a hold, not a fault. The breaker does
        # not open and no recovery is needed; the hold ends with the window.
        return [('held', 'declared', rec(gate_reason=declared.get('reason', '')))]
    if credential.get('red'):
        return [('degraded', 'credential', rec(gate_reason=credential.get('reason', '')))]
    if regulator.get('red'):
        return [('degraded', 'regulator_unverified', rec(gate_reason=regulator.get('reason', '')))]
    if canary.get('red'):
        return [('degraded', 'canary', rec(gate_reason=canary.get('reason', '')))]
    if fast['red']:
        return [('degraded', fast['code'], rec(gate_reason=fast['reason']))]
    if feeds:
        return [('awaiting_observation', 'awaiting_observation', rec())]
    from .queues import whole_node
    if whole_node(queue):
        # A whole-node queue is sized in nodes, not hours: each job holds a
        # node for its allocation, and an Event Service task over EVGEN input
        # is days of nodes (p4499: 14.5 million events, about 150 node-hours),
        # past any hours mark. The queue takes the next task while its own
        # work fills fewer nodes than its target, set with the queue's worker
        # ceiling (the harvester's; Google's pod cap, gke_pilots.max_pods):
        # its production jobs not started or running, the
        # files JEDI has not yet made jobs of, and at least one node for each
        # live task, which holds one between a job's end and the next.
        nodes = max(int(qs.get('nodes') or 1), 1)
        jobs = int(q.get('not_started_production') or 0) + sum(
            int((v or {}).get('production') or 0)
            for s, v in (q.get('by_status') or {}).items() if s in ('running', 'starting'))
        occupied = max(jobs + ungenerated, tasks_active)
        base.update(nodes=nodes, nodes_occupied=occupied)
        if occupied >= nodes:
            return [('supplied', 'node_occupied', rec())]
        if not eligible:
            return [('no_work', 'no_eligible_task', rec())]
        candidate = eligible[0]
        state = 'fed' if mode == 'active' else 'would_feed'
        return [(state, f"{state}:{candidate['task']}", rec(candidate))]
    if committed_h is None:
        return [('held', 'no_calibration', rec())]
    if committed_h >= h_low:
        return [('supplied', 'supplied', rec())]
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
            continue
        candidate_h = round(rows * median_h / ceiling, 2)
        # Phase one: everything a task declares is activated at once, so
        # one task past the high-water mark is the operator's call and
        # the job cap bounds the pool. Phase two: JEDI paces generation,
        # the pool is its to bound, and only the water marks and the
        # task cap remain.
        if not jedi_throttled and candidate_h > h_high:
            out.append(('oversize', 'oversize_task', rec(candidate, candidate_h=candidate_h)))
            continue
        if committed + candidate_h > h_high:
            out.append(('held', 'would_exceed_high',
                        rec(candidate, candidate_h=candidate_h,
                            committed_after=round(committed + candidate_h, 2))))
            continue
        if t_max and tasks_active + fed + 1 > t_max:
            out.append(('held', 'task_cap', rec(candidate, candidate_h=candidate_h)))
            break
        if not jedi_throttled and j_max and not_started + fed_rows + rows > j_max:
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


def hours_summary(backlog, latest, unplaced_queue=None):
    """The two quantities the front balances. Pure.

    ``ready``: the work queued in ready, in job-hours (one slot busy for
    one hour): each sizable task's jobs times its pinned queue's median
    walltime, by priority level and by queue, with the total.
    ``capacity``: each queue's capacity in job-hours per hour of clock,
    which is its ceiling (the most jobs it ran at once), with the total.
    Ready job-hours over total capacity is the clock time to drain the
    backlog with every queue full.
    """
    ready = {'by_priority': {'1': 0.0, '2': 0.0, '3': 0.0, 'unset': 0.0},
             'by_queue': {}, 'total': 0.0, 'unsized_tasks': 0}
    for queue, entries in backlog.items():
        # An unplaced task is sized at the queue first in line to draw.
        d = latest.get(unplaced_queue if queue == UNPLACED else queue) or {}
        median_h = d.get('median_walltime_h')
        for e in entries:
            rows = e.get('rows')
            if not rows or not median_h:
                ready['unsized_tasks'] += 1
                continue
            jh = rows * median_h
            key = str(e['level']) if e.get('level') in (1, 2, 3) else 'unset'
            ready['by_priority'][key] += jh
            ready['by_queue'][queue] = ready['by_queue'].get(queue, 0.0) + jh
            ready['total'] += jh
    capacity = {'by_queue': {}, 'by_pool': {}, 'total': 0}
    for queue, d in latest.items():
        ceiling = int(d.get('ceiling') or 0)
        capacity['by_queue'][queue] = ceiling
        pool = d.get('capacity_pool') or queue
        capacity['by_pool'][pool] = max(capacity['by_pool'].get(pool, 0), ceiling)
    capacity['total'] = sum(capacity['by_pool'].values())
    for k in list(ready['by_priority']):
        ready['by_priority'][k] = round(ready['by_priority'][k], 1)
    ready['by_queue'] = {q: round(v, 1) for q, v in ready['by_queue'].items()}
    ready['total'] = round(ready['total'], 1)
    drain_h = (round(ready['total'] / capacity['total'], 2)
               if capacity['total'] > 0 else None)
    return {'ready': ready, 'capacity': capacity, 'drain_h': drain_h}


# Placement (CONTINUOUS_PRODUCTION.md, Placement): where a task's work
# should go, from what the cycle stored. The Site control on the compose
# page and the front read the same recommendation.

CANARY_RANK = {'healthy': 0, 'insufficient': 1, 'unknown': 1, 'degraded': 2}
RANK_FAILURE_SHARE = 0.10          # a queue failing this share of its gate window ranks after cleaner ones
RANK_FAILURE_MIN_OUTCOMES = 50


def queue_limits(queue):
    """The queue's declared limits from schedconfig (a PanDA table read):
    walltime in hours, memory per job in MB and cores per job, each None
    when the queue declares none. None when the queue cannot be read."""
    from monitor_app.panda.queries import get_queue
    cfg = (get_queue(queue) or {}).get('queue')
    if not cfg:
        return None

    def _int(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    maxtime = _int(cfg.get('maxtime'))
    return {'maxtime_h': round(maxtime / 3600.0, 2) if maxtime else None,
            'maxrss_mb': _int(cfg.get('maxrss')) or None,
            'corecount': _int(cfg.get('corecount')) or None,
            'status': str(cfg.get('status') or '')}


def task_need(cfg, rows=None, *, event_service, es_bound=False, es_site=''):
    """What one job of a task asks of a queue, from its effective
    configuration, as the submission declares it (pcs/commands.py).
    ``event_service``: whether a whole-node queue can take the task
    (``evgen_es.capable``); every reader of a recommendation passes it,
    so the Site control and the feed judge whole-node queues alike.
    ``es_bound``: the task is Event Service work already
    (``event_service_bound``), which only a whole-node queue runs;
    ``es_site``: the one whole-node queue its setup was built for
    (``event_service_site``)."""
    from pcs.commands import task_container_image
    data = cfg.get('data') or {}
    hours = cfg.get('target_hours_per_job')
    walltime_h = float(hours) if hours is not None else float(data.get('walltime_hours', 2.0))
    cores = int(data.get('corecount') or 1)
    ram = int(data.get('ram_count') or 4096)
    return {'walltime_h': round(walltime_h, 2), 'cores': cores,
            'ram_mb_per_core': ram, 'memory_mb': ram * cores, 'rows': rows,
            'container_image': task_container_image(cfg),
            'event_service': bool(event_service), 'es_bound': bool(es_bound),
            'es_site': es_site or ''}


def whole_node_ready(task, cfg=None):
    """Whether a whole-node queue can take the task: Event Service work
    already (a feeder sample, an adopted request, a staged EVGEN task), or
    a registered EVGEN sample the feed stages for it (``evgen_es.capable``)."""
    from .evgen_es import capable
    return event_service_bound(task) or capable(task, cfg)


def event_service_bound(task):
    """Whether the task is Event Service work: it carries an Event Service
    setup (the ``event_service`` override), whose jobs need a whole node."""
    return bool((task.overrides or {}).get('event_service'))


def event_service_site(task):
    """The whole-node queue the task's Event Service setup was built for, or
    ''. A setup belongs to one site's nodes and input staging (evgen_es.SITES):
    its staging marker names the site (Perlmutter's input staged at NERSC,
    ``nersc_input``; Google's in the cluster's bucket, ``site_input``), else
    its job size does (``pilot_cores``, a Perlmutter node or a Google pod),
    else the queue the task is placed on, when that is a whole-node queue."""
    es = (task.overrides or {}).get('event_service') or {}
    if not es:
        return ''
    from .evgen_es import SITES
    marker = {'nersc': 'nersc_input', 'gcs': 'site_input'}
    for queue, (_shape, stager) in SITES.items():
        if es.get(marker.get(stager, '')):
            return queue
    for queue, (shape, _stager) in SITES.items():
        if es.get('pilot_cores') and es.get('pilot_cores') == shape.get('pilot_cores'):
            return queue
    from pcs.commands import pinned_site
    from .queues import whole_node
    placed = pinned_site(task)
    return placed if whole_node(placed) else ''


def event_service_misplaced(queue, task):
    """Why ``queue`` cannot take the task's Event Service work, or ''. Only
    a whole-node queue runs it: an Event Service job sent to a single-core
    queue dies before its payload starts (41525 and 41526 at BNL_OSG_PanDA_1,
    2026-10-29, pilot error 1310 on every job). And only the whole-node queue
    its setup was built for (``event_service_site``): the job shape and the
    staged input of Perlmutter's setup are not Google's, nor Google's
    Perlmutter's. An EVGEN setup older than the site-staged catalog input is
    refused at every whole-node queue. Read by the submit request and the
    Move (pcs/services.py)."""
    from .queues import whole_node
    if not queue or not event_service_bound(task):
        return ''
    if not whole_node(queue):
        return f'{queue} runs no Event Service work; {task.name} carries an Event Service setup'
    es = (task.overrides or {}).get('event_service') or {}
    if es.get('input') == 'evgen' and not (es.get('nersc_input') or es.get('site_input')):
        # An EVGEN setup from before the input was read from its catalog and
        # staged at the site, and closes named from the job's own file (an
        # earlier attempt's): never sent or moved to the queue again. 41525
        # and 41526 (p4495 and p4497 .try2), moved to Perlmutter with one on
        # 2026-10-29, named every close run001.
        return (f'{task.name}: its Event Service setup predates the catalog input staged at '
                f'the site; build it afresh with scripts/evgen-es-submit.py '
                f'(docs/NERSC_PERLMUTTER.md, EVGEN input in production)')
    built = event_service_site(task)
    if built and built != queue:
        return (f'{task.name} carries an Event Service setup built for {built}; '
                f'its job shape and staged input belong there, not at {queue}')
    return ''


def event_service_refusal(queue, need):
    """Why the queue cannot take the task, or ''. A whole-node queue: one
    job per whole node, Event Service work only (Torre Wenaus, 2026-10-28:
    "NO MORE SINGLE CORE 1/128 EFF WORK IN PERL EVER"). Any other queue:
    no Event Service work (``event_service_misplaced``). Read by
    ``recommend`` and the feed alike, so the Site control never recommends
    a queue the front would not feed the task to."""
    from .queues import whole_node
    if need is None:
        return ''
    if not whole_node(queue):
        if need.get('es_bound'):
            return f'{queue} runs no Event Service work; the task carries an Event Service setup'
        return ''
    if need.get('es_site') and need['es_site'] != queue:
        return f"{queue} cannot run the task's Event Service setup, built for {need['es_site']}"
    if need.get('event_service'):
        return ''
    return f'{queue} takes Event Service work only; not a registered EVGEN sample'


def fit_problems(need, limits):
    """Where one job of the task exceeds the queue's declared limits;
    brokerage finds no candidate for such a task. Pure."""
    problems = []
    if not need or not limits:
        return problems
    if limits.get('maxtime_h') and need['walltime_h'] > limits['maxtime_h']:
        problems.append(f"needs {need['walltime_h']} h per job, the queue allows "
                        f"{limits['maxtime_h']} h")
    if limits.get('maxrss_mb') and need['memory_mb'] > limits['maxrss_mb']:
        problems.append(f"needs {need['memory_mb']} MB per job, the queue allows "
                        f"{limits['maxrss_mb']} MB")
    if limits.get('corecount') and need['cores'] > limits['corecount']:
        problems.append(f"needs {need['cores']} cores, the queue's slots have "
                        f"{limits['corecount']}")
    return problems


def gate_blockers(gates, settings=None):
    """The gates standing between the queue and new work, as reasons.
    Pure; the same gates the decision reads, apart from the front's
    own switches (``front.enabled``, the queue's feed switch), which
    govern the front's feeding and not a person's placement."""
    if gates is None:
        return ['the front has not yet recorded this queue\'s gates']
    out = []
    breaker = str((settings or {}).get('breaker') or gates.get('breaker') or 'closed')
    if breaker != 'closed':
        out.append(f'breaker {breaker}')
    for key in ('declared', 'credential', 'canary', 'fast', 'regulator'):
        g = gates.get(key) or {}
        if g.get('red'):
            out.append(g.get('reason') or f'{key} gate red')
    return out


STALL_DEFAULT_H = 6.0
# A queue calibrated on a handful of fast starts reads a p90 of minutes;
# the bar never drops below this.
STALL_MIN_H = 1.0


def stall_signal(activity, p90_start_h):
    """Whether a PanDA task's waiting work is stuck where it is. Pure.

    ``activity``: ``queued`` and ``running`` job counts, ``oldest_wait_h``
    (hours the longest-waiting unstarted job has waited) and
    ``last_start_h`` (hours since any of its jobs started; None when none
    has). The bar is the queue's p90 start latency, else STALL_DEFAULT_H.
    ``stalled``: work waiting, none running, and nothing started for
    longer than the bar. ``slow``: jobs running, but the oldest waiting
    job has waited past the bar. Else ``''``.
    """
    a = activity or {}
    queued, running = int(a.get('queued') or 0), int(a.get('running') or 0)
    wait, last = a.get('oldest_wait_h'), a.get('last_start_h')
    bar = STALL_GRACE_H
    basis = f'{bar} h delivery grace'
    out = {'state': '', 'reason': '', 'bar_h': bar}
    if queued <= 0 or wait is None or wait <= bar:
        return out
    if running == 0 and (last is None or last > bar):
        started = ('no job has started' if last is None
                   else f'the last job started {last} h ago')
        out.update(state='stalled', reason=(
            f'{queued} jobs waiting, none running; {started}, and the oldest '
            f'has waited {wait} h, past {basis}'))
    else:
        out.update(state='slow', reason=(
            f'{running} jobs running, but {queued} waiting and the oldest has '
            f'waited {wait} h, past {basis}'))
    return out


def _depth_line(r):
    if r['committed_h'] is not None:
        return (f"{r['committed_h']} h of queued work against a low mark of "
                f"{r['h_low']} h")
    if r['not_started'] == 0:
        return 'no queued work'
    return f"{r['not_started']} jobs queued, depth in hours not yet calibrated"


def recommend(state, need, queues=None, closed=None, dispatch=None):
    """Rank production queues, reading current closures and pilot capability
    unless ``closed`` and ``dispatch`` are supplied for a pure calculation.

    ``state`` is the front's stored state (``front_state``), ``need`` the
    task's ``task_need``. A queue whose feed switch is off, with a red
    gate, or with a limit the task exceeds is not recommendable; a person
    may still choose it explicitly. The rest rank by canary standing
    (healthy first), then by committed depth in hours (least first; an
    uncalibrated queue with nothing queued counts as empty, one with jobs
    queued ranks after every known depth), then by p90 start latency
    (shortest first; none known last). Returns ``{'rows', 'recommended',
    'reason'}``; each row carries the census, the fit and the gates it
    was judged on.
    """
    state = state or {}
    decisions = state.get('queues') or {}
    placement = state.get('placement') or {}
    queues = list(queues or decisions.keys())
    closed = dict(closed_queues() if closed is None else closed)
    closed.update(RETIRED_QUEUES)
    from .queues import dispatch_capabilities, image_problem
    check_image = need is not None and 'container_image' in need
    if dispatch is None:
        dispatch = dispatch_capabilities() if check_image else {}
    rows = []
    for queue in queues:
        d = decisions.get(queue) or {}
        p = placement.get(queue) or {}
        limits = p.get('limits')
        committed_h = d.get('committed_h')
        not_started = int(d.get('not_started') or 0)
        median_h, ceiling = d.get('median_walltime_h'), int(d.get('ceiling') or 0)
        task_h = (round(need['rows'] * median_h / ceiling, 2)
                  if need and need.get('rows') and median_h and ceiling > 0 else None)
        gates = p.get('gates')
        row = {
            'queue': queue,
            'committed_h': committed_h, 'h_low': d.get('h_low'), 'h_high': d.get('h_high'),
            'not_started': not_started, 'running': int(d.get('running') or 0),
            'ceiling': ceiling, 'median_walltime_h': median_h,
            'p90_start_latency_h': d.get('p90_start_latency_h'),
            'task_h': task_h,
            'canary': ((gates or {}).get('canary') or {}).get('status') or d.get('canary') or 'unknown',
            'limits': limits,
            'fit': fit_problems(need, limits),
            'blockers': (gate_blockers(gates, {'breaker': d.get('breaker')})
                         + ([f'closed: {closed[queue]}'] if queue in closed else [])
                         + ([] if d.get('feed_switch') else
                            ['feed exempt' if d.get('feed_exempt') else 'feed off'])),
            'feed_off': not d.get('feed_switch'),
            'front_state': d.get('state', ''), 'front_reason': d.get('reason', ''),
        }
        image_issue = image_problem(need.get('container_image'), dispatch.get(queue)) if check_image else ''
        row['image_problem'] = image_issue
        if image_issue:
            row['fit'].append(image_issue)
        refusal = event_service_refusal(queue, need)
        if refusal:
            row['fit'].append(refusal)
        if limits is None:
            row['fit_unknown'] = True
        if committed_h is not None:
            row['depth_key'] = float(committed_h)
        elif not_started == 0:
            row['depth_key'] = 0.0
        else:
            row['depth_key'] = None
        # The work the queue lacks to reach its low mark, in slot-hours:
        # where idle slots come soonest, in proportion to the queue's size.
        # By depth in hours alone, 0.5 h on GREX's 949 slots ranked ahead
        # of 0.29 h on OSG's 4,425 while OSG ran out of work (26 October).
        h_low = float(d.get('h_low') or 0)
        row['need_slot_h'] = (round(max(0.0, h_low - row['depth_key']) * ceiling, 1)
                              if row['depth_key'] is not None and ceiling > 0 else None)
        # The share of the gate window's outcomes that failed: a queue
        # failing often ranks after the cleaner ones, well before its
        # failure-window gate (60%) holds it.
        finished, failed = int(d.get('gate_finished') or 0), int(d.get('gate_failed') or 0)
        row['failed_share'] = round(failed / (finished + failed), 3) if finished + failed else None
        row['failing'] = (finished + failed >= RANK_FAILURE_MIN_OUTCOMES
                          and row['failed_share'] >= RANK_FAILURE_SHARE)
        rows.append(row)

    def key(r):
        p90 = r['p90_start_latency_h']
        return (CANARY_RANK.get(r['canary'], 1),
                1 if r['failing'] else 0,
                -(r['need_slot_h'] or 0.0),
                r['depth_key'] if r['depth_key'] is not None else float('inf'),
                p90 if p90 is not None else float('inf'))
    eligible = sorted((r for r in rows if not r['blockers'] and not r['fit']), key=key)
    for rank, r in enumerate(eligible, 1):
        r['rank'] = rank
    best = eligible[0] if eligible else None
    if best is None:
        reason = ('no production queue is open to this task: every queue '
                  'has its feed off, a red gate or a limit the task exceeds')
        if not placement:
            reason = ('no recommendation yet: the front stores its gate readings '
                      'with each cycle, and none is stored')
        image_reasons = [r['queue'] + ': ' + r['image_problem'] for r in rows
                         if r['image_problem'] and r['queue'] not in closed and not r['feed_off']]
        if image_reasons:
            reason += '; ' + '; '.join(image_reasons)
        return {'rows': rows, 'recommended': '', 'reason': reason}
    bits = [_depth_line(best), f"canary {best['canary']}"]
    if best['need_slot_h']:
        bits.append(f"{best['need_slot_h']:,.0f} slot-hours short of its low mark")
    if best['failed_share'] is not None:
        bits.append(f"{best['failed_share']:.1%} of its recent outcomes failed")
    if best['p90_start_latency_h'] is not None:
        bits.append(f"p90 start latency {best['p90_start_latency_h']} h")
    if need:
        bits.append(f"fits {need['walltime_h']} h, {need['memory_mb']} MB, "
                    f"{need['cores']} core{'s' if need['cores'] != 1 else ''} per job"
                    + (' (limits unread)' if best.get('fit_unknown') else ''))
    return {'rows': rows, 'recommended': best['queue'],
            'reason': f"{best['queue']}: " + ', '.join(bits)}


# The feed

def feed_task(candidate_pk, created_by='front', site='', placement=None, kind='submit'):
    """Submit one ready or queued task with its kind's own submission: the
    PCS submit request (association source ``pcs_front_feed``), or a
    queued rerun's residual or entire-task request (their own sources,
    submitted by the front). Each allocates the attempt and enqueues the
    credentialed ``submit_evgen_task`` doer, and clears the queued mark.
    Raises on refusal (``ServiceError``: already submitted, nothing to
    rerun, agent queue unreachable) or a missing task; the caller records
    the refusal."""
    from pcs.models import ProdTask
    from pcs.services import (prodtask_rerun_entire_task_request,
                              prodtask_rerun_residual_request,
                              prodtask_submit_request)
    task = ProdTask.objects.get(pk=candidate_pk)
    staged = None
    if kind in ('submit', 'rerun_entire'):
        staged = _prepare_whole_node(task, site, created_by, placement)
    if kind == 'rerun_residual':
        prodtask_rerun_residual_request(task=task, site=site, placement=placement,
                                        changed_by=created_by)
    elif kind == 'rerun_entire':
        prodtask_rerun_entire_task_request(task=task, site=site, placement=placement,
                                           changed_by=created_by)
    elif kind == 'unplaced_move':
        from pcs.services import prodtask_place_unplaced
        prodtask_place_unplaced(task=task, site=site, changed_by=created_by,
                                placement=placement)
    else:
        prodtask_submit_request(task=task, changed_by=created_by, source=FEED_SOURCE,
                                site=site, placement=placement)
    if staged is not None:
        # The files JEDI leaves out of the task run as tasks of their own
        # (evgen_es.tails), queued with it.
        from pcs.services import prodtask_submit_tail
        from .evgen_es import tails
        task.refresh_from_db()
        for shape in tails(task, staged=staged):
            prodtask_submit_tail(task=task, event_service=shape, changed_by=created_by)
    return task


def _prepare_whole_node(task, site, created_by, placement):
    """For a task going to a whole-node queue, the queue it is fed to or the
    one it is already placed on (a placed task's feed names no site): its
    input read from its catalog, staged at the site, and its Event Service
    shape set afresh before the request (evgen_es.py), for a submission and a
    new attempt alike; the request refuses a whole-node queue any task
    without one. While the site's copy is made the task is placed on the
    queue, so no other queue takes it, and held (InputStaging). Returns the
    task's input as its catalog holds it, None for any other queue."""
    from pcs.commands import pinned_site
    from .queues import whole_node
    from .evgen_es import SITES
    target = site or pinned_site(task)
    if not whole_node(target) and target not in SITES:
        return None
    from .evgen_es import InputStaging, prepare
    from .gke_pilots import settings as gke_settings
    try:
        return prepare(task, gke_settings()['proxy'], site=target)
    except InputStaging:
        if pinned_site(task) != target:
            from pcs.services import prodtask_place
            prodtask_place(task, target, changed_by=created_by, placement=placement)
        raise


def _apply_feeds(outcomes, *, created_by):
    """Active mode: perform each ``fed`` decision. A refused feed becomes
    ``error (feed_failed)`` carrying the refusal; it is not a feed on the
    record, so the next cycle decides the queue again."""
    applied = []
    for state, reason, record in outcomes:
        if state != 'fed':
            applied.append((state, reason, record))
            continue
        pk = record.get('candidate_pk')
        try:
            if pk is None:
                raise ValueError('the candidate carries no task pk')
            # An unplaced candidate is placed on the queue that fed it.
            placed = record.get('candidate_placement')
            feed_task(pk, created_by=created_by,
                      site=record.get('queue', '') if placed else '',
                      placement=placed, kind=record.get('candidate_kind') or 'submit')
        except Exception as exc:  # noqa: BLE001
            from .evgen_es import InputStaging
            if isinstance(exc, InputStaging):
                # A hold, not a fault: the input is being copied to the site
                # the task is placed on, and it starts when there.
                applied.append(('held', 'input_staging', dict(record, detail=str(exc))))
                continue
            logger.exception('front: feed of %s (pk %s) refused', record.get('candidate'), pk)
            applied.append(('error', 'feed_failed',
                            dict(record, error=f'{type(exc).__name__}: {exc}')))
            continue
        applied.append((state, reason, dict(record, submitted=True)))
    return applied


# The cycle

REGULATOR_ALARM_KEY = 'front_regulator_alarm'


def regulator_alarm(regulator, now, created_by):
    """One alarm per episode of unverified regulation, and one recovery.

    While the job throttler's decisions cannot be verified the gate holds
    every queue the front feeds (``regulator_unverified``). On 2026-10-28
    it did so from 16:13 to 19:08 ET, after the throttler's decision lines
    changed under the reporter that reads them, and nothing alarmed;
    Torre found it. Held past ``front.regulator_alarm_after_s`` (600), the
    episode is one ``front_regulator_unverified`` action at severity
    alarm, again if its reason changes: epicprod-live publishes it, and
    the notify-llm subscription brings it to the AI sessions on the host
    (swf-monitor NOTICE_ROUTING.md). Its end is one recovery record."""
    from monitor_app.epicprod_logging import log_epicprod_action
    from monitor_app.models import PersistentState
    after_s = float(setting('front.regulator_alarm_after_s', 600))
    state = dict((PersistentState.get_state() or {}).get(REGULATOR_ALARM_KEY) or {})
    reason = str(regulator.get('reason') or '')
    if regulator.get('red'):
        since = state.get('since') or now.isoformat()
        held_s = (now - datetime.fromisoformat(since)).total_seconds()
        if held_s >= after_s and (not state.get('alarmed') or state.get('reason') != reason):
            # Plain words (Torre Wenaus, 2026-10-30: the record was "ONE
            # HUNDRED PERCENT INCOMPREHENSIBLE"): what is held, for how
            # long, and why, in the reader's terms.
            log_epicprod_action(
                'front', 'front_regulator_unverified', username=created_by,
                subject_type='front', subject_key='regulator', outcome='error',
                sublevel='high', live_default=True, level=logging.ERROR,
                title='Pressure front holding new work at every site',
                subject_label='Pressure front',
                message=(f'Pressure front holding new work at every site for '
                         f'{int(held_s // 60)} min: {reason}'),
                severity='alarm', reason=reason, held_s=int(held_s),
                narration=(f'No new work has gone to any site for {int(held_s // 60)} min. '
                           f'The front releases work only while it sees JEDI\'s job '
                           f'throttler deciding; {reason}.'
                           + (' JEDI on pandaserver01 has most likely stopped.'
                              if 'no JEDI throttler decision' in reason else '')))
            state['alarmed'] = True
        state.update(since=since, reason=reason)
    elif state.get('alarmed'):
        held_s = (now - datetime.fromisoformat(state['since'])).total_seconds()
        log_epicprod_action(
            'front', 'front_regulator_unverified', username=created_by,
            subject_type='front', subject_key='regulator', outcome='ok',
            sublevel='high', live_default=True,
            title='Pressure front releasing new work again',
            subject_label='Pressure front',
            message=f'Pressure front releasing new work again after {int(held_s // 60)} min',
            severity='info', held_s=int(held_s),
            narration=(f'JEDI\'s job throttler is seen deciding again; new work was held '
                       f'at every site for {int(held_s // 60)} min.'))
        state = {}
    else:
        state = {}
    PersistentState.update_state({REGULATOR_ALARM_KEY: state})


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
        logger.error('front.mode %r is not one of %s; deciding as shadow',
                     mode_requested, MODES)
        errors.append(f'front.mode {mode_requested!r} is not one of {MODES}; decided as shadow')
        mode = 'shadow'
    jedi_throttled = bool(setting('front.jedi_throttled', False))
    regulator = {'red': False}
    if jedi_throttled:
        def check_regulator():
            from django.db import connections
            from monitor_app.host_reports import latest
            with connections['panda'].cursor() as cursor:
                cursor.execute("SELECT vo, value FROM doma_panda.config WHERE component=%s "
                               "AND app=%s AND key=%s", ['epic_job_throttler', 'jedi', 'MODE'])
                modes = dict(cursor.fetchall())
            return throttler_verdict(latest('pandaserver01'), modes, now=t0)
        regulator = _safe('job regulator', check_regulator, lambda exc: {
            'red': True, 'reason': f'job regulator unreadable: {exc}'})
    if not dry_run:
        _safe('regulator alarm', lambda: regulator_alarm(regulator, t0, created_by),
              failed('regulator alarm'))
    queues = regulated_queues()
    from swf_epicprod import task_failure_guard
    task_guard = _safe('task failure guard', lambda: task_failure_guard.run(
        queues, enabled=enabled, mode=mode, dry_run=dry_run, created_by=created_by,
        # The mailed rule: shadow until its day has run (CONTINUOUS_PRODUCTION.md,
        # Task failure guard); the first rule acts until front.task_guard.legacy is off.
        rolling_mode=str(setting('front.task_guard.mode', 'shadow')),
        rolling_cfg=task_failure_guard.rolling_settings(setting),
        legacy=bool(setting('front.task_guard.legacy', True))),
        failed('task failure guard')) or []
    # A feed switched off with an automatic return comes back on when its
    # criterion holds, before this cycle reads the queue settings.
    from swf_epicprod import feed_guard
    feed_returns = _safe('feed guard', lambda: feed_guard.run(
        queues, dry_run=dry_run, created_by=created_by), failed('feed guard')) or []
    max_per_cycle = max(1, int(setting('front.max_per_cycle', 2) or 1))
    activation_window_s = int(setting('front.activation_window_s', 600))

    # The monitor's queue inventory follows PanDA's queue table, so a queue
    # added in PanDA is on every queue list within one cycle.
    if not dry_run:
        from monitor_app.panda.queue_inventory import sync_queue_inventory
        _safe('queue inventory', sync_queue_inventory, failed('queue inventory'))

    census = _safe('census', queue_census, failed('census'))
    # A queue under an operator core limit is sized at the limit.
    limits = _safe('core limits', lambda: core_limits(_config()), lambda exc: {})
    census = core_limited(census, limits)
    backlog = _safe('backlog', ready_backlog, failed('backlog'))
    if backlog is None:
        backlog = {}
    credential = _safe('credential gate', _credential_gate, lambda exc: {
        'outcome': 'unread', 'age_h': None, 'red': True,
        'reason': f'credential check unreadable: {exc}'})

    decisions, written = [], 0
    settings_by_queue = {}
    placement = {}
    for queue in queues:
        settings = _safe(f'{queue} settings', lambda: queue_settings(queue),
                         lambda exc: dict(QUEUE_DEFAULTS))
        # An unreadable waiver reads as none: the fast detectors stand.
        settings['gate_waiver'] = _safe(f'{queue} gate waiver', lambda: gate_waiver(queue),
                                        lambda exc: None)
        settings_by_queue[queue] = settings
        canary = _safe(f'{queue} canary gate', lambda: _canary_gate(queue), lambda exc: {
            'status': 'unread', 'age_h': None, 'red': True,
            'reason': f'canary unreadable: {exc}'})
        # An unreadable declared record reads as no declaration: the
        # measured gates still stand between the front and a dead queue.
        declared = _safe(f'{queue} declared gate',
                         lambda: _declared_gate(queue, settings['h_high']),
                         lambda exc: {'red': False, 'state': 'unread',
                                      'reason': f'declared record unreadable: {exc}'})
        census_q = (census or {}).get('queues', {}).get(queue) if census else None
        # What placement reads (recommend): the gates as judged, whatever
        # the front's own switches decide, and the queue's limits.
        placement[queue] = {
            'gates': {'canary': canary, 'credential': credential, 'declared': declared,
                      'regulator': regulator,
                      'fast': fast_detectors((census_q or {}).get('gate'),
                                             settings.get('gate_waiver')),
                      'breaker': settings.get('breaker')},
            'limits': _safe(f'{queue} limits', lambda: queue_limits(queue), None),
        }

    # Ready tasks hold no queue (CONTINUOUS_PRODUCTION.md, The front's
    # placement): the unplaced ones form one pool, and a queue the front
    # may feed (switched on and calibrated) draws on it when it decides
    # to feed, the neediest first. The ranking reads the previous cycle's
    # stored decisions with this cycle's switches, gates and limits.
    feedable = [q for q in queues
                if enabled and settings_by_queue[q].get('feed')
                and ((census or {}).get('queues', {}).get(q) or {})
                .get('hours_at_capacity') is not None]
    pool = backlog.pop(UNPLACED, [])
    pool = _safe('lapsed unplace marks', lambda: lapse_unplaced(
        pool, created_by=created_by, dry_run=dry_run), lambda exc: pool)
    stored = _safe('stored decisions', _stored_decisions, lambda exc: {})
    draw_state = {'queues': {q: dict(stored.get(q) or {},
                                     feed_switch=bool(settings_by_queue[q].get('feed')))
                             for q in queues},
                  'placement': placement}
    order = _safe('draw order', lambda: draw_order(queues, feedable, draw_state),
                  lambda exc: list(queues))

    for queue in order:
        settings = settings_by_queue[queue]
        gates = placement[queue]['gates']
        canary, declared = gates['canary'], gates['declared']
        pool_members = next((members for members in CAPACITY_POOLS.values() if queue in members), (queue,))
        feeds = _safe(f'{queue} feeds', lambda: [f for member in pool_members
                       for f in _recent_feeds(member, activation_window_s)], [])
        last_feed_age_h = _safe(f'{queue} last feed', lambda: _last_feed_age_h(queue), None)
        census_q = (census or {}).get('queues', {}).get(queue) if census else None
        if census is None:
            outcomes = [('held', 'census_unavailable',
                         {'queue': queue, 'mode': mode, 'ready_total': len(backlog.get(queue, [])),
                          'ready_eligible': 0, 'h_low': settings['h_low'], 'h_high': settings['h_high']})]
        else:
            drawn = (_safe(f'{queue} ready pool', lambda: open_to(pool, queue, draw_state),
                           lambda exc: []) if queue in feedable else [])
            candidates = sorted(backlog.get(queue, []) + drawn, key=ENTRY_ORDER)
            outcomes = _safe(
                f'{queue} decision',
                lambda: decide(queue, census_q, candidates, settings,
                               {'canary': canary, 'credential': credential,
                                'declared': declared, 'regulator': regulator}, feeds,
                               enabled=enabled, mode=mode, max_per_cycle=max_per_cycle,
                               last_feed_age_h=last_feed_age_h,
                               jedi_throttled=jedi_throttled),
                lambda exc: [('error', 'decision_error',
                              {'queue': queue, 'mode': mode, 'error': f'{type(exc).__name__}: {exc}',
                               'h_low': settings['h_low'], 'h_high': settings['h_high'],
                               'ready_total': len(backlog.get(queue, [])), 'ready_eligible': 0})])
            if mode == 'active' and not dry_run:
                outcomes = _apply_feeds(outcomes, created_by=created_by)
            # A task this queue took leaves the pool for its queue.
            taken = {r.get('candidate_pk') for st, _reason, r in outcomes if st in FEED_STATES}
            for entry in drawn:
                if entry['pk'] in taken:
                    backlog.setdefault(queue, []).append(entry)
            pool = [e for e in pool if e['pk'] not in taken]
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
                         f'{record.get("ready_total", 0)} ready'
                         + (f'; {record["error"]}' if record.get('error') else '')),
                **{k: v for k, v in record.items() if k != 'queue'}), failed(f'{queue} record'))
            written += 1

    if pool:
        backlog[UNPLACED] = left_in_pool(pool, feedable)
    latest = {}
    for d in decisions:
        latest[d['queue']] = d
    hours = _safe('hours summary', lambda: hours_summary(
        backlog, latest, unplaced_queue=next(iter(feedable and order), None)),
        failed('hours summary'))

    # A queue with no ready work takes unstarted tasks waiting at another
    # (CONTINUOUS_PRODUCTION.md, Idle-queue move).
    from swf_epicprod import idle_move
    idle = _safe('idle-queue move', lambda: idle_move.run(
        queues, latest, placement, enabled=enabled, front_mode=mode,
        cfg=idle_move.settings(setting), dry_run=dry_run, created_by=created_by),
        failed('idle-queue move')) or {}

    # Pilot-side regulation (CONTINUOUS_PRODUCTION.md), shadow mode: the
    # harvester limits each regulated queue would carry, recorded every
    # cycle for comparison with the realized running count.
    from swf_epicprod import pilots as pilot_regulation
    previous_pilots = _safe('stored pilot state', _stored_pilots, {})
    pilot_decisions = _safe('pilot decisions', lambda: pilot_regulation.run(
        census, placement, previous_pilots, now=t0, errors=errors),
        failed('pilot decisions')) or {}
    for queue, p in pilot_decisions.items():
        if dry_run:
            continue
        would = p.get('would_set') or {}
        p['log_id'] = _safe(f'{queue} pilot record', lambda: log_epicprod_action(
            'front', 'pilot_decision', subject_type='panda_queue',
            subject_key=queue, username=created_by, outcome=p['state'],
            sublevel='normal' if p['state'] == 'error' else 'low',
            message=(f'pilots {queue}: {p["state"]} ({p["reason"]}); '
                     f'running {p.get("running", "?")} of target {p.get("target_running")}, '
                     f'queued {p.get("queued", "?")} of {p.get("queued_target", "?")}'
                     + (f'; would set {would}' if p['state'] == 'would_set' else '')
                     + (f'; {p["error"]}' if p.get('error') else '')),
            **{k: v for k, v in p.items() if k not in ('queue', 'log_id')}),
            failed(f'{queue} pilot record'))
    summary = {'observed_at': (census or {}).get('observed_at') if census else None,
               'mode': mode, 'mode_requested': mode_requested, 'enabled': enabled,
               'jedi_throttled': jedi_throttled,
               'task_failure_guard': task_guard,
               'feed_guard': feed_returns,
               'idle_move': idle,
               'queues': len(queues), 'decisions_recorded': written,
               'errors': errors,
               'states': {q: d['state'] for q, d in latest.items()},
               'pilot_states': {q: p['state'] for q, p in pilot_decisions.items()},
               'duration_s': round((timezone.now() - t0).total_seconds(), 1)}
    if not dry_run:
        state_payload = {'observed_at': summary['observed_at'], 'cycle_at': t0.isoformat(),
                         'mode': mode, 'mode_requested': mode_requested, 'enabled': enabled,
                         'jedi_throttled': jedi_throttled,
                         'queues': latest, 'placement': placement,
                         'pilots': pilot_decisions, 'idle_move': idle,
                         'backlog': backlog, 'errors': errors,
                         'hours': hours, 'duration_s': summary['duration_s']}
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
