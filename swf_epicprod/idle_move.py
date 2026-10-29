"""Move unstarted production tasks to an idle production queue.

When the front has nothing in ``ready`` for a queue (``no_work``) and the
queue sits below its low mark, a task waiting unstarted at another
production queue goes there instead (Torre Wenaus, 2026-10-26: "in such a
situation, i want such a move to be automatic"). The move is the Site
control's Move (``pcs.services.prodtask_move_request``): PanDA's soft
reassign through the operations agent, which repays the attempt each
closed job cost. Only tasks with nothing started are moved, so no pilot
loses a job, and moves stop once the two queues are level, so work does
not travel back and forth (docs/CONTINUOUS_PRODUCTION.md, Idle-queue move).
"""

SOURCE = 'front_idle_move'
MODES = ('active', 'shadow', 'off')
DEFAULTS = {'mode': 'active', 'max_per_cycle': 4, 'cooldown_h': 12.0}
MOVE_FAILURE_LIMIT = 0.20       # an idle queue takes moved work only below this failure fraction
RECENT_MINUTES = 60             # ... over the gate window and over this last hour
RECENT_MIN_OUTCOMES = 20        # the last hour is read when it holds this many finished and failed
# Paused tasks wait for an operator's resume, which is not ours to make.
TASK_STATUSES = ('defined', 'ready', 'running', 'scouting', 'scouted', 'pending',
                 'assigning', 'throttled')
# A job in any of these has a pilot or is past one; a task with one stays.
STARTED_JOB_STATUSES = ('sent', 'starting', 'running', 'holding', 'transferring',
                        'merging')


def settings(setting):
    """The rule's SysConfig settings, ``front.idle_move.<name>``."""
    out = {k: setting(f'front.idle_move.{k}', v) for k, v in DEFAULTS.items()}
    out['mode'] = str(out['mode'])
    out['max_per_cycle'] = max(0, int(out['max_per_cycle'] or 0))
    out['cooldown_h'] = float(out['cooldown_h'] or 0)
    return out


def failure_fraction(decision):
    """The queue's failed jobs over its finished and failed in the front's
    gate window, failures a finding explains included: a queue failing for
    a known reason is failing. None when the window holds no outcome. Pure."""
    d = decision or {}
    if d.get('gate_finished') is None or d.get('gate_failed') is None:
        return None
    failed = int(d['gate_failed']) + int(d.get('gate_explained') or 0)
    outcomes = int(d['gate_finished']) + failed
    return failed / outcomes if outcomes else None


def refused_for_failures(decision, recent=None):
    """Why an idle queue takes no moved work for its failures, or ''.
    Torre Wenaus, 2026-10-26, after the front moved task 41349's 500 jobs to
    GREX while 2,696 of its 7,343 jobs in the gate window had failed (37%,
    under the front's 60% red): "NO MOVE UNLESS FAILURE FRACTION BELOW 20%".
    A fraction that cannot be read does not establish it. The same limit
    holds over the last RECENT_MINUTES (``recent``: finished, failed), read
    when they hold RECENT_MIN_OUTCOMES: a burst that resumes after a quiet
    spell (GREX, 178 copy failures in the 20 minutes after its feed came back
    at 16:58) is diluted in the six-hour window. Pure."""
    fraction = failure_fraction(decision)
    d = decision or {}
    if fraction is None:
        return 'no finished or failed job in the gate window to read its failure fraction'
    if fraction >= MOVE_FAILURE_LIMIT:
        failed = int(d['gate_failed']) + int(d.get('gate_explained') or 0)
        return (f'failure fraction {fraction:.0%} ({failed} failed of {failed + int(d["gate_finished"])} '
                f'in the gate window), not below {MOVE_FAILURE_LIMIT:.0%}')
    finished, failed = recent or (0, 0)
    if finished + failed >= RECENT_MIN_OUTCOMES and failed / (finished + failed) >= MOVE_FAILURE_LIMIT:
        return (f'failure fraction {failed / (finished + failed):.0%} ({failed} failed of '
                f'{finished + failed} in the last {RECENT_MINUTES} minutes), not below {MOVE_FAILURE_LIMIT:.0%}')
    return ''


def recent_outcomes(queues, now=None):
    """(finished, failed) of the production jobs that ended at each queue in
    the last RECENT_MINUTES, from PanDA's archive."""
    from datetime import timedelta

    from django.db import connections
    from django.utils import timezone
    from monitor_app.panda.constants import PANDA_SCHEMA

    out = {q: (0, 0) for q in queues}
    if not out:
        return out
    since = (now or timezone.now()) - timedelta(minutes=RECENT_MINUTES)
    with connections['panda'].cursor() as cursor:
        cursor.execute(
            "SELECT computingsite, COUNT(*) FILTER (WHERE jobstatus = 'finished'), "
            "COUNT(*) FILTER (WHERE jobstatus = 'failed') "
            f'FROM "{PANDA_SCHEMA}".jobsarchived4 WHERE computingsite = ANY(%s) AND endtime > %s '
            "AND processingtype = 'epicproduction' GROUP BY 1", [list(out), since])
        for queue, finished, failed in cursor.fetchall():
            out[queue] = (int(finished or 0), int(failed or 0))
    return out


def idle_queues(decisions):
    """The queues that need work the ready pool cannot give them: the
    front found nothing eligible, the queue is below its low mark, its
    feed is on and its canary is healthy. Pure."""
    out = {}
    for queue, d in (decisions or {}).items():
        if (d.get('state') == 'no_work' and d.get('committed_h') is not None
                and d.get('h_low') is not None and d['committed_h'] < d['h_low']
                and d.get('canary') == 'healthy' and d.get('feed_switch')):
            out[queue] = d
    return out


def idle_targets(decisions, recent=None):
    """The idle queues that may take moved work: those whose failure
    fraction is below MOVE_FAILURE_LIMIT (``recent``: recent_outcomes). Pure."""
    return {q: d for q, d in idle_queues(decisions).items()
            if not refused_for_failures(d, (recent or {}).get(q))}


def task_hours(rows, decision):
    """Hours ``rows`` jobs take at a queue's capacity, the depth measure
    (CONTINUOUS_PRODUCTION.md, The pressure measure); None uncalibrated."""
    median, ceiling = (decision or {}).get('median_walltime_h'), int((decision or {}).get('ceiling') or 0)
    if not rows or not median or ceiling <= 0:
        return None
    return round(rows * float(median) / ceiling, 3)


def choose(decisions, tasks, *, max_moves, recent=None):
    """The moves to make this cycle. Pure.

    ``decisions``: this cycle's decision per queue. ``tasks``: candidates,
    each with ``jeditaskid``, ``site``, ``priority``, ``queued`` (jobs not
    started), ``ungenerated``, ``started`` (jobs with a pilot), ``eligible``
    (the queues the recommender finds open to it: no blocker, no fit
    problem) and ``recent_move`` (moved within the cooldown).

    A task moves from its queue S to an idle queue T when nothing of it
    has started, T is open to it, T is still below its low mark, and the
    move leaves T no deeper than S: ``T + task at T <= S - task at S``,
    in hours at each queue's capacity. Tasks with no jobs generated go
    first (the move closes nothing), then lower priority, then newer:
    the work that would start last where it is. Returns ``(moves,
    skipped)``; each move names ``from``, ``to``, ``rows`` and the hours.
    """
    targets = idle_targets(decisions, recent)
    moves, skipped = [], []
    if not targets or max_moves <= 0:
        return moves, skipped
    depth = {q: d.get('committed_h') for q, d in (decisions or {}).items()}
    order = sorted(tasks, key=lambda t: (int(t.get('queued') or 0) > 0,
                                         int(t.get('priority') or 0),
                                         -int(t['jeditaskid'])))
    for task in order:
        if len(moves) >= max_moves:
            break
        source = task.get('site')
        rows = int(task.get('queued') or 0) + int(task.get('ungenerated') or 0)
        why = ''
        # The source may be idle too (GREX on 2026-10-26: nothing ready for
        # either queue); the depth condition alone gives the direction.
        if int(task.get('started') or 0) > 0:
            continue
        if rows <= 0:
            continue
        if task.get('recent_move'):
            why = 'moved within the cooldown'
        source_h = depth.get(source)
        x_source = task_hours(rows, decisions.get(source))
        if not why and (source_h is None or x_source is None):
            why = f'{source} depth uncalibrated'
        best = None
        if not why:
            for queue in sorted(targets, key=lambda q: (depth[q], q)):
                if queue == source or queue not in (task.get('eligible') or ()):
                    continue
                x_target = task_hours(rows, decisions[queue])
                if x_target is None or depth[queue] >= decisions[queue]['h_low']:
                    continue
                if depth[queue] + x_target <= source_h - x_source:
                    best = (queue, x_target)
                    break
            if best is None:
                # A task already at the shallowest idle queue is not news.
                shallower = sorted(q for q in targets if q != source and depth[q] < source_h)
                if not shallower:
                    continue
                why = ('no idle queue open to it would stay shallower than its own'
                       if any(q in (task.get('eligible') or ()) for q in shallower)
                       else 'not open to it (image, limits or gates): ' + ', '.join(shallower))
        if why:
            skipped.append({'jeditaskid': task['jeditaskid'], 'site': source, 'why': why})
            continue
        queue, x_target = best
        before = {'from_h': source_h, 'to_h': depth[queue]}
        depth[source] = round(source_h - x_source, 3)
        depth[queue] = round(depth[queue] + x_target, 3)
        moves.append({'jeditaskid': task['jeditaskid'], 'taskname': task.get('taskname', ''),
                      'prod_task_pk': task.get('prod_task_pk'),
                      'from': source, 'to': queue, 'rows': rows,
                      'queued': int(task.get('queued') or 0),
                      'from_task_h': x_source, 'to_task_h': x_target, **before,
                      'from_after_h': depth[source], 'to_after_h': depth[queue]})
    return moves, skipped


def candidates(queues, cooldown_h):
    """Production tasks at the regulated queues that the rule may move,
    with their job counts and ungenerated rows. Propagates read errors."""
    from datetime import timedelta

    from django.db import connections
    from django.utils import timezone
    from monitor_app.models import PandaTaskOperation
    from monitor_app.panda.census import ungenerated_by_task
    from monitor_app.panda.constants import PANDA_SCHEMA

    if not queues:
        return []
    with connections['panda'].cursor() as cursor:
        cursor.execute(
            f'SELECT jeditaskid, taskname, status, site, taskpriority '
            f'FROM "{PANDA_SCHEMA}".jedi_tasks WHERE site = ANY(%s) AND status = ANY(%s) '
            "AND processingtype = 'epicproduction' AND prodsourcelabel = 'managed' "
            'AND COALESCE(eventservice, 0) = 0',
            [list(queues), list(TASK_STATUSES)])
        tasks = {row[0]: dict(zip(('jeditaskid', 'taskname', 'status', 'site', 'priority'), row),
                              queued=0, started=0)
                 for row in cursor.fetchall()}
        if not tasks:
            return []
        cursor.execute(
            'SELECT jeditaskid, jobstatus, count(*) FROM ('
            f'SELECT jeditaskid, jobstatus FROM "{PANDA_SCHEMA}".jobsactive4 WHERE jeditaskid = ANY(%s) '
            'UNION ALL '
            f'SELECT jeditaskid, jobstatus FROM "{PANDA_SCHEMA}".jobsdefined4 WHERE jeditaskid = ANY(%s)'
            ') j GROUP BY 1, 2', [list(tasks), list(tasks)])
        for tid, status, n in cursor.fetchall():
            tasks[tid]['started' if status in STARTED_JOB_STATUSES else 'queued'] += int(n)
    ungenerated = ungenerated_by_task()
    since = timezone.now() - timedelta(hours=cooldown_h)
    recent = set(PandaTaskOperation.objects.filter(
        jedi_task_id__in=list(tasks), operation='reassign', requested_at__gte=since)
        .values_list('jedi_task_id', flat=True))
    for tid, task in tasks.items():
        task['ungenerated'] = (ungenerated.get(tid) or (None, 0))[1]
        task['recent_move'] = tid in recent
    return list(tasks.values())


def _prod_task(jeditaskid):
    """The PCS task whose current PanDA task this is, or None; a trial is
    None, since its queue is the point of it."""
    from pcs.models import PandaTasks
    row = (PandaTasks.objects.filter(jedi_task_id=jeditaskid)
           .select_related('prod_task', 'prod_task__dataset').first())
    task = row.prod_task if row else None
    if task is None or str(task.panda_task_id or '') != str(jeditaskid):
        return None
    if ((task.dataset.metadata if task.dataset is not None else None) or {}).get('trial'):
        return None
    return task


def _eligible(task, state, queues, closed, dispatch):
    """The queues the recommender finds open to the PCS task."""
    from pcs.services import _declared_rows
    from swf_epicprod.front import recommend, task_need, whole_node_ready
    cfg = task.get_effective_config()
    result = recommend(state, task_need(cfg, rows=_declared_rows(task, cfg),
                                        event_service=whole_node_ready(task, cfg)), queues,
                       closed=closed, dispatch=dispatch)
    return [r['queue'] for r in result['rows'] if not r['blockers'] and not r['fit']]


def run(queues, decisions, placement, *, enabled, front_mode, cfg, dry_run, created_by):
    """One front cycle of the rule. Acts only when the front is enabled
    and active and the rule's mode is ``active``; ``shadow`` reports what
    it would move. Returns ``{'mode', 'moves', 'skipped'}``."""
    from monitor_app.epicprod_logging import log_epicprod_action
    from pcs.services import ServiceError, prodtask_move_request
    from swf_epicprod.front import closed_queues
    from swf_epicprod.queues import dispatch_capabilities

    idle = idle_queues(decisions)
    recent = recent_outcomes(list(idle)) if idle else {}
    out = {'mode': cfg['mode'], 'moves': [], 'skipped': [],
           'refused': [{'queue': q, 'why': refused_for_failures(d, recent.get(q)),
                        'failure_fraction': failure_fraction(d), 'last_hour': recent.get(q)}
                       for q, d in sorted(idle.items()) if refused_for_failures(d, recent.get(q))]}
    if not enabled or cfg['mode'] not in ('active', 'shadow') or not idle_targets(decisions, recent):
        return out
    state = {'queues': decisions, 'placement': placement}
    closed, dispatch = closed_queues(), dispatch_capabilities()
    tasks = []
    for t in candidates(queues, cfg['cooldown_h']):
        if t['started'] or not (t['queued'] or t['ungenerated']):
            continue
        prod = _prod_task(t['jeditaskid'])
        if prod is None:
            continue
        t['prod_task_pk'] = prod.pk
        t['eligible'] = _eligible(prod, state, queues, closed, dispatch)
        tasks.append(t)
    moves, out['skipped'] = choose(decisions, tasks, max_moves=cfg['max_per_cycle'], recent=recent)
    acting = enabled and front_mode == 'active' and cfg['mode'] == 'active' and not dry_run
    for move in moves:
        reason = (f"{move['to']} idle (no ready work, {move['to_h']} h queued against its "
                  f"low mark); {move['rows']} unstarted jobs waiting at {move['from']} "
                  f"({move['from_h']} h queued); after the move {move['from_after_h']} h "
                  f"and {move['to_after_h']} h")
        move['reason'] = reason
        if not acting:
            move['outcome'] = 'would_move'
            continue
        from pcs.models import ProdTask
        try:
            record = prodtask_move_request(
                task=ProdTask.objects.get(pk=move['prod_task_pk']), site=move['to'],
                changed_by=created_by, source=SOURCE,
                placement={'recommended': move['to'], 'reason': reason})
        except (ServiceError, ProdTask.DoesNotExist) as exc:
            move['outcome'] = 'refused'
            move['error'] = getattr(exc, 'detail', None) or str(exc)
            continue
        move['outcome'] = 'move_requested'
        move['operation_id'] = record.get('id')
        log_epicprod_action(
            'front', 'idle_move', subject_type='panda_task',
            subject_key=str(move['jeditaskid']), subject_label=move['taskname'],
            username=created_by, outcome='move_requested', sublevel='normal',
            live_default=True,
            message=f"Moving {move['taskname']} from {move['from']} to {move['to']}: {reason}",
            **{k: v for k, v in move.items() if k not in ('taskname', 'outcome')})
    out['moves'] = moves
    return out
