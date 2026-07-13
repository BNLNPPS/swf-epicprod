"""Campaign status rollup and mechanical verdict floor
(docs/EPICPROD_ASSESSMENTS_V1.md).

``campaign_status`` composes every analytics member into one evidence
document and computes the verdict floor before any model runs. Served
by the ``epicprod_campaign_status`` MCP tool and
``GET /pcs/api/campaigns/status/`` — peer surfaces over this one
function.
"""

import datetime as _dt

from django.utils import timezone

from . import members as _members

VERDICTS = ('ok', 'attention', 'alarm')


def _worst(*verdicts):
    return VERDICTS[max(VERDICTS.index(v) for v in verdicts)]


def producing_campaigns():
    """Campaigns with fresh Rucio arrivals — the derived 'producing'
    status (EPICPROD_DATA_LINEAGE.md): an arrivals block recorded within
    SysConfig ``campaign_producing_window_days`` (default 3, covering
    missed sweep nights). Current-labeled campaigns are excluded — the
    Current tab already is their surface. Returns [(campaign, arrivals),
    ...] sorted by name; purely derived, no stored lifecycle involved.
    """
    from monitor_app.models import SysConfig
    from pcs.models import Campaign

    days = SysConfig.get_setting('campaign_producing_window_days', 3)
    try:
        window = _dt.timedelta(days=float(days))
    except (TypeError, ValueError):
        window = _dt.timedelta(days=3)
    cutoff = timezone.now() - window
    out = []
    for camp in Campaign.objects.exclude(lifecycle='current'):
        arrivals = (camp.data or {}).get('arrivals') or {}
        try:
            last = _dt.datetime.fromisoformat(
                arrivals.get('last_arrival_at', ''))
        except (TypeError, ValueError):
            continue
        if last.tzinfo is None:
            last = last.replace(tzinfo=_dt.timezone.utc)
        if last >= cutoff:
            out.append((camp, arrivals))
    return sorted(out, key=lambda pair: pair[0].name)


def resolve_target_campaigns():
    """Assessment targets: every producing campaign, else the current one."""
    from pcs.models import Campaign

    targets = [camp.name for camp, _ in producing_campaigns()]
    current = Campaign.objects.filter(lifecycle='current').first()
    if current and current.name not in targets:
        targets.append(current.name)
    return targets


def _floor(blocks):
    """The mechanical verdict floor: ok | attention | alarm with reasons.

    Thresholds live in SysConfig at defaults (no hidden knobs); the model
    may raise the verdict with justification, never lower it below this.
    """
    from monitor_app.models import SysConfig

    ffail_attention = float(SysConfig.get_setting('assessment_ffail_attention', 0.10))
    ffail_alarm = float(SysConfig.get_setting('assessment_ffail_alarm', 0.30))
    sync_stale_hours = float(SysConfig.get_setting('assessment_sync_stale_hours', 26))
    stall_days = float(SysConfig.get_setting('assessment_arrivals_stall_days', 2))

    verdict = 'ok'
    reasons = []

    # The floor alarms on the window, not the lifetime: an unchanged
    # accumulated failure burden must not re-alarm every night. Lifetime
    # figures ride as standing context for the report.
    window = blocks.get('window_activity', {}).get('data', {})
    rate = window.get('window_failure_rate')
    terminal = (window.get('window_jobs_finished') or 0) + \
               (window.get('window_jobs_failed') or 0)
    if rate is not None and terminal >= 20:
        if rate >= ffail_alarm:
            verdict = _worst(verdict, 'alarm')
            reasons.append(f'window failure rate {rate:.1%} >= {ffail_alarm:.0%} '
                           f'({terminal} terminal jobs in window)')
        elif rate >= ffail_attention:
            verdict = _worst(verdict, 'attention')
            reasons.append(f'window failure rate {rate:.1%} >= {ffail_attention:.0%} '
                           f'({terminal} terminal jobs in window)')

    activity = blocks['action_stream_activity']['data']
    sync_age = activity.get('catalog_sync_age_hours')
    if sync_age is None:
        verdict = _worst(verdict, 'attention')
        reasons.append('no catalog_sync record found')
    elif sync_age > sync_stale_hours:
        verdict = _worst(verdict, 'attention')
        reasons.append(f'catalog_sync {sync_age:.0f}h old > {sync_stale_hours:.0f}h')
    elif activity.get('catalog_sync_outcome') not in ('', 'ok'):
        verdict = _worst(verdict, 'attention')
        reasons.append(f"last catalog_sync outcome "
                       f"'{activity.get('catalog_sync_outcome')}'")

    arrivals = blocks['rucio_arrivals']['data']
    progress = blocks['campaign_progress']['data']
    age_hours = arrivals.get('last_arrival_age_hours')
    incomplete = (progress.get('available')
                  and progress.get('outputs_complete', 0)
                  < progress.get('outputs_total', 0))
    if age_hours is not None and incomplete and age_hours > stall_days * 24:
        verdict = _worst(verdict, 'attention')
        reasons.append(f'no arrivals for {age_hours / 24:.1f}d with '
                       f'incomplete outputs')

    creds = blocks['credential_status']['data']
    if not creds.get('available'):
        verdict = _worst(verdict, 'attention')
        reasons.append('credential status unknown (no check record)')
    elif creds.get('outcome') not in ('', 'ok'):
        text = f"{creds.get('outcome')} {creds.get('reason')} " \
               f"{creds.get('message')}".lower()
        cred_verdict = ('alarm' if ('expired' in text or 'missing' in text)
                        else 'attention')
        verdict = _worst(verdict, cred_verdict)
        reasons.append(f"credential check outcome '{creds.get('outcome')}'")

    system = blocks['system_status']['data']
    if not system.get('available'):
        verdict = _worst(verdict, 'attention')
        reasons.append('production platform status unavailable')
    else:
        platform = str(system.get('overall_status') or 'unknown').lower()
        if platform == 'error':
            verdict = _worst(verdict, 'alarm')
            reasons.append('production platform status error')
        elif platform not in ('ok', 'healthy'):
            verdict = _worst(verdict, 'attention')
            reasons.append(f'production platform status {platform}')

    return {'verdict': verdict, 'reasons': reasons,
            'standing_context': {
                'lifetime_final_failure_rate':
                    blocks['panda_health']['data'].get('final_failure_rate'),
            }}


def _record_status_snapshot(campaign, status, generated_by):
    """Persist one analytics computation as production history."""
    from monitor_app.epicprod_logging import log_epicprod_action

    row_id = log_epicprod_action(
        'campaign-analytics', 'campaign_analytics_snapshot',
        subject_type='campaign', subject_key=campaign.name,
        username=str(generated_by or '')[:100], outcome='ok',
        sublevel='low', live_default=False,
        message=f'campaign analytics snapshot {campaign.name}',
        status=status,
    )
    if row_id is None:
        raise RuntimeError('campaign analytics snapshot could not be recorded')


def campaign_status_snapshot(campaign, target_at):
    """Return the recorded campaign state closest to ``target_at``.

    Snapshot history belongs to production analytics. This query does not
    inspect assessment prompts or generated reports.
    """
    from monitor_app.models import AppLog
    from pcs.models import Campaign
    from pcs.services import ServiceError

    try:
        camp = Campaign.objects.get(name=str(campaign or '').strip())
    except Campaign.DoesNotExist:
        raise ServiceError(f'Unknown campaign {campaign!r}.', status=404)
    try:
        target = _dt.datetime.fromisoformat(
            str(target_at or '').replace('Z', '+00:00'))
    except (TypeError, ValueError):
        raise ServiceError('history_at must be an ISO-8601 timestamp.', status=400)
    if target.tzinfo is None:
        target = target.replace(tzinfo=_dt.timezone.utc)

    snapshots = AppLog.objects.filter(
        app_name='epicprod',
        extra_data__action='campaign_analytics_snapshot',
        extra_data__subject_type='campaign',
        extra_data__subject_key=camp.name,
    )
    before = snapshots.filter(timestamp__lte=target).order_by(
        '-timestamp', '-id').first()
    after = snapshots.filter(timestamp__gt=target).order_by(
        'timestamp', 'id').first()
    choices = [row for row in (before, after) if row is not None]
    if not choices:
        return {
            'available': False,
            'campaign': camp.name,
            'target_at': target.isoformat(),
            'reason': 'no recorded campaign analytics snapshots',
        }
    selected = min(
        choices,
        key=lambda row: abs((row.timestamp - target).total_seconds()))
    extra = selected.extra_data if isinstance(selected.extra_data, dict) else {}
    status = extra.get('status') if isinstance(extra.get('status'), dict) else {}
    selected_at = str(status.get('generated_at') or selected.timestamp.isoformat())
    selected_dt = _dt.datetime.fromisoformat(selected_at.replace('Z', '+00:00'))
    if selected_dt.tzinfo is None:
        selected_dt = selected_dt.replace(tzinfo=_dt.timezone.utc)
    return {
        'available': True,
        'campaign': camp.name,
        'target_at': target.isoformat(),
        'selected_at': selected_at,
        'distance_hours': round(
            abs((selected_dt - target).total_seconds()) / 3600, 3),
        'generated_by': str(extra.get('username') or ''),
        'status': status,
    }


def campaign_status(campaign=None, window_days=1, *, record=False,
                    generated_by=''):
    """Build the campaign status evidence document.

    campaign: name string, or None for the default target (first
    producing campaign, else current). window_days bounds the activity
    window for deltas, flips, and action aggregation.
    """
    from monitor_app.models import SysConfig
    from pcs.models import Campaign
    from pcs.services import ServiceError

    targets = resolve_target_campaigns()
    name = str(campaign or '').strip() or (targets[0] if targets else '')
    if not name:
        raise ServiceError('No producing or current campaign to assess.',
                           status=404)
    try:
        camp = Campaign.objects.get(name=name)
    except Campaign.DoesNotExist:
        raise ServiceError(f'Unknown campaign {name!r}.', status=404)

    try:
        window_days = max(float(window_days), 0.04)  # floor ~1 hour
    except (TypeError, ValueError):
        window_days = 1.0
    window_end = timezone.now()
    window_start = window_end - _dt.timedelta(days=window_days)

    blocks = {}
    for member in _members.MEMBERS:
        blocks[member.__name__] = member(camp, window_start, window_end)

    status = {
        'schema_version': 1,
        'campaign': camp.name,
        'lifecycle': camp.lifecycle or '',
        'generated_at': window_end.isoformat(),
        'window_days': window_days,
        'window': {'start': window_start.isoformat(),
                   'end': window_end.isoformat()},
        'targets': targets,
        'assessment_enabled': bool(
            SysConfig.get_setting('assessment_enabled', True)),
        'members': blocks,
        'floor': _floor(blocks),
    }
    if record:
        _record_status_snapshot(camp, status, generated_by)
    return status
