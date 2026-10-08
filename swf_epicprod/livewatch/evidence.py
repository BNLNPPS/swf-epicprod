"""The live watch's evidence bundle, deterministic (docs/EPICPROD_ASSESSMENTS.md,
The live watch): the channel's posts, the action record's failures and
recoveries grouped by action, component and cause, the flapping counts,
the publication policy in force, and the mechanical floor.

Runs under Django (the action record is read from AppLog directly); the
channel is read with the assessment bundle's reader, GETs only.
"""
import hashlib
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from swf_epicprod.assessment.bundle import _live_findings, _Manifest
from swf_epicprod.livewatch import spec

BUNDLE_SCHEMA = 'epicprod-livewatch-bundle/1'
FAILURE_OUTCOMES = ('error', 'timeout', 'partial', 'unrecorded')
MAX_GROUPS = 60
EXAMPLES = 3


def cause_of(text):
    """A failure's cause with its particulars folded, so one cause with
    different files, ids and counts groups as one."""
    parts = []
    # A record naming several items repeats one cause per item; fold the
    # repeats so the count of items does not split the group.
    for part in str(text or '').split('; '):
        part = re.sub(r'(?:root|https?)://\S+', '<url>', part.strip())
        part = re.sub(r'/\S+', '<path>', part)
        part = re.sub(r'\b[0-9a-f]{8,}\b', '<id>', part)
        part = re.sub(r'\d+(?:\.\d+)?', '#', part)
        part = re.sub(r'\s+', ' ', part)
        if part and part not in parts:
            parts.append(part)
    return '; '.join(parts)[:160]


def _key(action, instance, cause=None):
    base = f'{action}@{instance}'
    if cause is None:
        return base
    # Reasons are cut at 300 characters when recorded, so a long cause ends
    # at different points; the key reads its opening only.
    return f"{base}~{hashlib.sha1(cause[:100].encode()).hexdigest()[:8]}"


def failure_groups(rows, now):
    """Group failure records by action, component and cause. ``rows`` are
    (timestamp, instance, extra_data, message). Pure."""
    groups = {}
    day_ago = now - timedelta(hours=24)
    for ts, instance, extra, message in rows:
        extra = extra or {}
        if extra.get('outcome') not in FAILURE_OUTCOMES:
            continue
        action = str(extra.get('action') or '')
        cause = cause_of(extra.get('reason') or extra.get('summary') or message)
        key = _key(action, instance, cause)
        g = groups.setdefault(key, {'key': key, 'action': action, 'instance': instance,
                                    'cause': cause, 'outcomes': defaultdict(int),
                                    'count_24h': 0, 'count_7d': 0, 'days': set(),
                                    'first': ts, 'last': ts, 'examples': [], 'subjects': set(),
                                    'requesters': set()})
        g['count_7d'] += 1
        g['outcomes'][extra.get('outcome')] += 1
        g['days'].add(ts.date())
        if ts >= day_ago:
            g['count_24h'] += 1
        g['first'], g['last'] = min(g['first'], ts), max(g['last'], ts)
        if extra.get('subject_key'):
            g['subjects'].add(str(extra['subject_key']))
        if extra.get('username'):
            g['requesters'].add(str(extra['username']))
        if len(g['examples']) < EXAMPLES:
            g['examples'].append({'at': ts.isoformat(), 'outcome': extra.get('outcome'),
                                  'reason': str(extra.get('reason') or message or '')[:400]})
    out = []
    for g in groups.values():
        out.append({
            'key': g['key'], 'action': g['action'], 'instance': g['instance'], 'cause': g['cause'],
            'count_24h': g['count_24h'], 'count_7d': g['count_7d'], 'days_7d': len(g['days']),
            'outcomes': dict(g['outcomes']), 'first': g['first'].isoformat(),
            'last': g['last'].isoformat(), 'subjects': sorted(g['subjects'])[:10],
            'subject_count': len(g['subjects']), 'requesters': sorted(g['requesters'])[:5],
            'examples': g['examples'],
        })
    out.sort(key=lambda g: (-g['count_24h'], -g['days_7d'], -g['count_7d']))
    return out[:MAX_GROUPS]


def flapping(rows, now):
    """Per action and component, how often a failure was followed by a
    success in the last 24 h. Pure; ``rows`` in time order."""
    day_ago = now - timedelta(hours=24)
    last = {}
    turns = defaultdict(int)
    for ts, instance, extra, _message in rows:
        if ts < day_ago:
            continue
        extra = extra or {}
        outcome = extra.get('outcome')
        if outcome not in FAILURE_OUTCOMES and outcome != 'ok':
            continue
        key = _key(str(extra.get('action') or ''), instance)
        failed = outcome in FAILURE_OUTCOMES
        if last.get(key) is True and not failed:
            turns[key] += 1
        last[key] = failed
    return sorted(({'key': k, 'turns_24h': n} for k, n in turns.items() if n),
                  key=lambda f: -f['turns_24h'])


def _policy():
    from monitor_app import live_notices
    return {
        'document': 'swf-monitor docs/NOTICE_ROUTING.md, Human-channel publication policy',
        'maintenance_quiet': sorted(live_notices.MAINTENANCE),
        'routine_successes_quiet': sorted(live_notices.ROUTINE_SUCCESSES),
        'rule': ('Automated maintenance passes never reach the channel, failures and '
                 'recoveries included. Other automated failures post on first sight and '
                 'when their cause changes; a recovery posts once. Human-requested '
                 'actions are not quieted.'),
    }


def assemble(now=None, window_hours=spec.WINDOW_HOURS):
    """The bundle for one watch run."""
    from monitor_app.models import AppLog

    now = now or datetime.now(timezone.utc)
    manifest = _Manifest()
    channel = _live_findings(now, 1.0, manifest)
    window_start = now - timedelta(hours=window_hours)
    for post in channel.get('posts') or []:
        post['in_window'] = post['timestamp'] >= window_start.isoformat()

    qs = (AppLog.objects.filter(app_name='epicprod', timestamp__gte=now - timedelta(days=7))
          .exclude(extra_data__isnull=True).order_by('timestamp')
          .values_list('timestamp', 'instance_name', 'extra_data', 'message'))
    failures = [r for r in qs.filter(extra_data__outcome__in=FAILURE_OUTCOMES).iterator()]
    failing_actions = {(r[2] or {}).get('action') for r in failures}
    recent = [r for r in qs.filter(timestamp__gte=now - timedelta(hours=24),
                                   extra_data__action__in=[a for a in failing_actions if a]).iterator()]
    manifest.note('action_record', True, f'{len(failures)} failure records in 7 d')

    groups = failure_groups(failures, now)
    flaps = flapping(recent, now)
    verdict, reasons = spec.floor(groups, flaps)
    return {
        'schema': BUNDLE_SCHEMA,
        'generated_at': now.isoformat(timespec='seconds'),
        'window': {'start': window_start.isoformat(timespec='seconds'),
                   'end': now.isoformat(timespec='seconds'), 'hours': window_hours},
        'channel_hours': 24,
        'channel': channel,
        'failure_groups': groups,
        'flapping': flaps,
        'policy': _policy(),
        'floor': {'verdict': verdict, 'reasons': reasons,
                  'keys': [r.split(':', 1)[0] for r in reasons],
                  'rules': {'repeat_24h': spec.REPEAT_24H, 'flap_24h': spec.FLAP_24H,
                            'days_7d': spec.DAYS_7D}},
        'manifest': manifest.entries,
        'degraded': manifest.degraded,
    }
