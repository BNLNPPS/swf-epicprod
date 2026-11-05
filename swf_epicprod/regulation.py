"""Shared identities and evidence checks for production regulation."""
from datetime import datetime, timezone

from swf_epicprod import queues as _registry

# Admission and accounting are separate. A retired queue's existing tasks
# drain in place; their jobs still share their pool's capacity budget. Both
# are live views of the queue registry (docs/QUEUE_REGISTRY.md), read on
# each use: {queue: reason} and {pool: (queue, ...)}.
RETIRED_QUEUES = _registry.LiveMapping(_registry.retired)
CAPACITY_POOLS = _registry.LiveMapping(_registry.pools)
PILOTS_PER_WORKER = {'UM_GREX_PanDA_1': 8}
THROTTLER_EVIDENCE_MAX_AGE_S = 600


def require_task_destination(queue):
    """Guard new task creation only; never job generation or task retries."""
    reason = RETIRED_QUEUES.get(str(queue or '').strip())
    if reason:
        raise ValueError(f'{queue}: {reason}')


#: The VOs whose throttler decisions the front requires, named for readers:
#: production tasks carry VO wlcg, the test paths VO epic (distcomp-services
#: panda-server/UPGRADE.md, ePIC modules in JEDI).
VO_NAMES = {'wlcg': 'production (wlcg)', 'epic': 'test (epic)'}


def _et(at):
    """An aware datetime as HH:MM ET, with the date when not today."""
    from zoneinfo import ZoneInfo
    local = at.astimezone(ZoneInfo('America/New_York'))
    today = datetime.now(ZoneInfo('America/New_York')).date()
    return local.strftime('%H:%M ET' if local.date() == today else '%m-%d %H:%M ET')


def throttler_verdict(report, modes, *, now=None):
    """A switch is insufficient: require fresh successful runtime decisions.
    Each reason is in plain words, for the alarm and the Production now line
    that show it (Torre Wenaus, 2026-10-30: the alarm record was
    "ONE HUNDRED PERCENT INCOMPREHENSIBLE")."""
    now = now or datetime.now(timezone.utc)
    reasons = []
    age = (report or {}).get('age_seconds')
    if age is None or age > THROTTLER_EVIDENCE_MAX_AGE_S:
        reasons.append('the pandaserver01 report is missing or more than '
                       f'{THROTTLER_EVIDENCE_MAX_AGE_S // 60} min old')
    evidence = ((report or {}).get('record') or {}).get('jedi_regulation') or {}
    if evidence.get('error'):
        reasons.append(evidence['error'])
    unread = evidence.get('unread') or {}
    mode = modes.get('epic') if modes.get('epic') is not None else modes.get('wlcg')
    if mode != 'throttle':
        reasons.append('the ePIC job throttler is not set to throttle in JEDI')
    for vo in ('wlcg', 'epic'):
        item = (evidence.get('voices') or {}).get(vo) or {}
        at = None
        try:
            at = datetime.fromisoformat(item['at'].replace('Z', '+00:00'))
            if not 0 <= (now - at).total_seconds() <= THROTTLER_EVIDENCE_MAX_AGE_S:
                raise ValueError('stale')
        except (KeyError, ValueError, TypeError, AttributeError):
            since = f'since {_et(at)}' if at is not None else 'recorded'
            reasons.append(f'{VO_NAMES[vo]}: no JEDI throttler decision {since}')
    if reasons and unread.get('count'):
        # Decision lines the reporter could not read explain a red verdict
        # (a changed line format, named as such) but never make one: an old
        # unread line in the log tail must not hold queues once decisions verify.
        reasons.append(f"{unread['count']} throttler decision lines in the JEDI log "
                       f"could not be read (latest: {str(unread.get('latest', ''))[-160:]})")
    return {'red': bool(reasons), 'reason': '; '.join(reasons), 'evidence': evidence}
