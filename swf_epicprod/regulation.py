"""Shared identities and evidence checks for production regulation."""
from datetime import datetime, timezone

CAPACITY_POOLS = {'osg': ('BNL_OSG_PanDA_1', 'BNL_OSG_EPIC_PROD_1')}
PILOTS_PER_WORKER = {'UM_GREX_PanDA_1': 8}
THROTTLER_EVIDENCE_MAX_AGE_S = 600


def throttler_verdict(report, modes, *, now=None):
    """A switch is insufficient: require fresh successful runtime decisions."""
    now = now or datetime.now(timezone.utc)
    reasons = []
    age = (report or {}).get('age_seconds')
    if age is None or age > THROTTLER_EVIDENCE_MAX_AGE_S:
        reasons.append('PanDA server report missing or stale')
    evidence = ((report or {}).get('record') or {}).get('jedi_regulation') or {}
    if evidence.get('error'):
        reasons.append(evidence['error'])
    for vo in ('wlcg', 'epic'):
        if modes.get(vo) != 'throttle':
            reasons.append(f'{vo} MODE is not throttle')
        item = (evidence.get('voices') or {}).get(vo) or {}
        try:
            at = datetime.fromisoformat(item['at'].replace('Z', '+00:00'))
            if not 0 <= (now - at).total_seconds() <= THROTTLER_EVIDENCE_MAX_AGE_S:
                raise ValueError('stale')
        except (KeyError, ValueError, TypeError):
            reasons.append(f'{vo} has no fresh verified throttler decision')
    return {'red': bool(reasons), 'reason': '; '.join(reasons), 'evidence': evidence}
