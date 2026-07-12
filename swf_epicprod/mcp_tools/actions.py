"""MCP retrieval for the epicprod action stream (app_name='epicprod' in AppLog)."""

from collections import defaultdict

from asgiref.sync import sync_to_async

from monitor_app.epicprod_logging import EPICPROD_APP_NAME
from monitor_app.mcp import mcp
from monitor_app.mcp.common import _parse_time, _default_start_time, _monitor_url

# Aggregation reads at most this many rows; log volumes beyond it are
# reported via truncated=True so summaries are never silently partial.
SUMMARIZE_MAX_ROWS = 5000

# Importance ordering for the sublevel floor (EPICPROD_ACTION_STREAM.md).
SUBLEVEL_ORDER = ('low', 'normal', 'high')


def _apply_sublevel_floor(qs, floor):
    # 'normal' excludes only declared-low records, so records without a
    # sublevel pass through rather than vanish; 'high' requires the
    # declaration.
    if floor == 'normal':
        return qs.exclude(extra_data__sublevel='low')
    if floor == 'high':
        return qs.filter(extra_data__sublevel='high')
    return qs


def _item(log):
    extra = log.extra_data if isinstance(log.extra_data, dict) else {}
    known = {
        'id': log.id,
        'timestamp': log.timestamp.isoformat() if log.timestamp else None,
        'instance': log.instance_name,
        'action': extra.get('action') or log.funcname,
        'subject_type': extra.get('subject_type') or '',
        'subject_key': extra.get('subject_key') or '',
        'username': extra.get('username') or '',
        'outcome': extra.get('outcome') or '',
        'reason': extra.get('reason') or '',
        'duration_ms': extra.get('duration_ms'),
        'sublevel': extra.get('sublevel') or '',
        'live_default': bool(extra.get('live_default')),
        'message': log.message,
    }
    counts = {
        key: value for key, value in extra.items()
        if key not in ('action', 'subject_type', 'subject_key', 'username',
                       'outcome', 'reason', 'duration_ms', 'sublevel',
                       'live_default')
    }
    if counts:
        known['counts'] = counts
    return known


def _list_actions_sync(action, instance, subject_type, subject_key, username,
                       outcome, start_time, end_time, min_sublevel, summarize,
                       limit, offset):
    from monitor_app.models import AppLog

    if min_sublevel is not None and min_sublevel not in SUBLEVEL_ORDER:
        return {
            'success': False,
            'error': f"min_sublevel must be one of {', '.join(SUBLEVEL_ORDER)}; "
                     f"got {min_sublevel!r}",
        }

    qs = AppLog.objects.filter(app_name=EPICPROD_APP_NAME).order_by('-timestamp')
    if action:
        qs = qs.filter(extra_data__action=action)
    if instance:
        qs = qs.filter(instance_name=instance)
    if subject_type:
        qs = qs.filter(extra_data__subject_type=subject_type)
    if subject_key:
        qs = qs.filter(extra_data__subject_key=subject_key)
    if username:
        qs = qs.filter(extra_data__username=username)
    if outcome:
        qs = qs.filter(extra_data__outcome=outcome)

    start = _parse_time(start_time) or _default_start_time(24 if not summarize else 24 * 7)
    end = _parse_time(end_time)
    qs = qs.filter(timestamp__gte=start)
    if end:
        qs = qs.filter(timestamp__lte=end)

    # Importance floor: broad raw listings default to 'normal' so routine
    # low-importance mechanics never drown the real actions; a drill-down
    # on action/subject_key/outcome, and summarize (which aggregates), see
    # everything unless a floor is passed explicitly.
    if min_sublevel is None:
        broad_listing = not summarize and not action and not subject_key and not outcome
        effective_floor = 'normal' if broad_listing else 'low'
    else:
        effective_floor = min_sublevel

    if effective_floor == 'low':
        total_count = qs.count()
        excluded_below_floor = 0
    else:
        pre_floor_count = qs.count()
        qs = _apply_sublevel_floor(qs, effective_floor)
        total_count = qs.count()
        excluded_below_floor = pre_floor_count - total_count

    result = {
        'success': True,
        'total_count': total_count,
        'min_sublevel': effective_floor,
        'excluded_below_floor': excluded_below_floor,
        'window_start': start.isoformat(),
        'window_end': end.isoformat() if end else None,
        'monitor_urls': [
            {'title': 'epicprod action log',
             'url': _monitor_url(f'/logs/?app_name={EPICPROD_APP_NAME}')},
        ],
    }

    if summarize:
        rows = list(qs[:SUMMARIZE_MAX_ROWS])
        by_action = defaultdict(lambda: {
            'count': 0, 'ok': 0, 'error': 0, 'other': 0,
            'durations_ms': [],
        })
        by_instance = defaultdict(int)
        for log in rows:
            extra = log.extra_data if isinstance(log.extra_data, dict) else {}
            key = extra.get('action') or log.funcname or '(none)'
            bucket = by_action[key]
            bucket['count'] += 1
            outcome_value = str(extra.get('outcome') or '')
            if outcome_value == 'ok':
                bucket['ok'] += 1
            elif outcome_value == 'error':
                bucket['error'] += 1
            else:
                bucket['other'] += 1
            duration = extra.get('duration_ms')
            if isinstance(duration, (int, float)):
                bucket['durations_ms'].append(duration)
            by_instance[log.instance_name] += 1

        actions_summary = {}
        for key, bucket in sorted(by_action.items()):
            durations = bucket.pop('durations_ms')
            if durations:
                bucket['duration_ms'] = {
                    'avg': round(sum(durations) / len(durations)),
                    'max': max(durations),
                    'min': min(durations),
                    'timed_count': len(durations),
                }
            actions_summary[key] = bucket

        result.update({
            'summary': {
                'by_action': actions_summary,
                'by_instance': dict(sorted(by_instance.items())),
            },
            'truncated': total_count > SUMMARIZE_MAX_ROWS,
        })
        return result

    safe_limit = max(1, min(int(limit or 50), 200))
    safe_offset = max(0, int(offset or 0))
    result.update({
        'items': [_item(log) for log in qs[safe_offset:safe_offset + safe_limit]],
        'limit': safe_limit,
        'offset': safe_offset,
        'has_more': total_count > safe_offset + safe_limit,
    })
    return result


@mcp.tool()
async def epicprod_list_actions(
    action: str = None,
    instance: str = None,
    subject_type: str = None,
    subject_key: str = None,
    username: str = None,
    outcome: str = None,
    start_time: str = None,
    end_time: str = None,
    min_sublevel: str = None,
    summarize: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """
    Query the epicprod ACTION stream: one structured record per production
    action (sweeps, submissions, button actions, assessments, reports),
    recording who did what to what, the outcome, and measured duration.

    START HERE — common questions, one call each:
    - What happened recently?           epicprod_list_actions(summarize=True)
    - Did the sweeps run, how long?     epicprod_list_actions(action='catalog_sync', summarize=True)
    - Any failed actions today?         epicprod_list_actions(outcome='error')
    - History of one task:              epicprod_list_actions(subject_key='<composed task name>')
    - What has the ops agent done?      epicprod_list_actions(instance='ops-agent')
    - Include routine mechanics too:    epicprod_list_actions(min_sublevel='low')

    Raw listings apply an importance floor of 'normal' by default, so
    high-volume low-importance mechanics (e.g. the 5-minute
    system_status_refresh) never drown the real actions; what the floor
    excluded is counted in excluded_below_floor, never silently dropped.
    The floor lifts automatically when action, subject_key, or outcome is
    specified (a drill-down must not return a misleading empty set) and for
    summarize=True (aggregation collapses volume into counts).

    summarize=True answers "what ran and how did it go" directly: counts by
    action with ok/error split and duration statistics (avg/max/min ms), and
    counts by component. Prefer it over reading raw items whenever you are
    reporting or assessing. Default window: 24 hours for listing, 7 days for
    summaries; pass start_time to widen.

    Use swf_list_logs for raw process/infrastructure logs; use this tool for
    the action history.

    Args:
        action: Action identifier filter (e.g. 'rucio_sweep', 'task_submit',
            'assessment_register').
        instance: Component filter ('web', 'ops-agent', 'mcp', 'catalog-sync',
            'submit', 'report').
        subject_type: Acted-on object type (assessment subject types where
            applicable, e.g. 'campaign_task', 'campaign', 'panda_queue').
        subject_key: Acted-on object key (composed task name, campaign name,
            JEDI task id, queue name).
        username: Human or service account that drove the action.
        outcome: 'ok' or 'error'.
        start_time: ISO timestamp or relative like '-24h', '-7d'.
        end_time: ISO timestamp; open-ended if omitted.
        min_sublevel: Importance floor ('low' < 'normal' < 'high'); records
            below it are excluded and counted in excluded_below_floor. Pass
            explicitly to override the default described above in either
            direction.
        summarize: Return aggregate counts and duration stats instead of items.
        limit: Max items when listing (default 50, cap 200).
        offset: Pagination offset when listing.

    Returns:
        total_count, the effective min_sublevel, excluded_below_floor, and
        window always; with summarize=True a summary block (by_action with
        ok/error and duration_ms stats, by_instance) and a truncated flag;
        otherwise items (promoted structured fields plus any recorded
        counts), limit/offset/has_more.
    """
    return await sync_to_async(_list_actions_sync)(
        action=action,
        instance=instance,
        subject_type=subject_type,
        subject_key=subject_key,
        username=username,
        outcome=outcome,
        start_time=start_time,
        end_time=end_time,
        min_sublevel=min_sublevel,
        summarize=summarize,
        limit=limit,
        offset=offset,
    )
