"""Assessment slot freshness (docs/EPICPROD_ASSESSMENTS.md, Harness
Lifecycle): a scheduled slot that never fills must surface as an alarm.

Registration is the last step of the chain — trigger, corun run,
completion callback, enforcement — and the completion handler cannot
report a job that never came back. This check therefore reads the
``assessment_register`` events in the action stream — the same local
record the catalog-sync freshness reads (analytics/members.py) — and
ages the newest registration per target campaign against the schedule,
so a run lost anywhere upstream goes red on the System page instead of
silently missing. Thresholds are SysConfig knobs at visible defaults.
"""

from django.utils import timezone

# Pre-rename registrations carry kind 'nightly'; they are daily assessments.
DAILY_KINDS = ('daily', 'nightly')


def assessment_freshness():
    """(status, summary, data) for the System page collector.

    Per target campaign (analytics.rollup.resolve_target_campaigns), the
    age of the newest registered daily and weekly assessment. A daily
    older than SysConfig ``assessment_daily_stale_hours`` (default 26,
    one 03:45 slot plus grace) or a weekly older than
    ``assessment_weekly_stale_hours`` (default 170, one Monday 06:00
    slot plus grace) is an error. A target with no daily at all is a
    warning — its first slot is pending. A target with no weekly at all
    is noted, not alarmed, until the first weekly registers. A
    quarantined registration fills its slot: quarantine is a visible
    outcome with its own error event, not a missing one.
    """
    from monitor_app.models import AppLog, SysConfig

    from swf_epicprod.analytics.rollup import resolve_target_campaigns

    daily_stale = float(
        SysConfig.get_setting('assessment_daily_stale_hours', 26))
    weekly_stale = float(
        SysConfig.get_setting('assessment_weekly_stale_hours', 170))
    now = timezone.now()
    errors, warnings, notes = [], [], []
    detail = {}
    targets = resolve_target_campaigns()
    for name in targets:
        rows = {}
        for kind, kinds, stale_hours in (
                ('daily', DAILY_KINDS, daily_stale),
                ('weekly', ('weekly',), weekly_stale)):
            latest = (AppLog.objects
                      .filter(app_name='epicprod',
                              extra_data__action='assessment_register',
                              extra_data__outcome='ok',
                              extra_data__subject_key=name,
                              extra_data__assessment_kind__in=list(kinds))
                      .order_by('-timestamp')
                      .values('timestamp', 'extra_data')
                      .first())
            if latest is None:
                rows[kind] = {'registered': False}
                if kind == 'daily':
                    warnings.append(f'{name}: no daily assessment yet')
                else:
                    notes.append(f'{name}: no weekly yet')
                continue
            age_hours = round(
                (now - latest['timestamp']).total_seconds() / 3600, 1)
            extra = (latest['extra_data']
                     if isinstance(latest['extra_data'], dict) else {})
            rows[kind] = {
                'registered': True,
                'registered_at': latest['timestamp'].isoformat(),
                'age_hours': age_hours,
                'verdict': str(extra.get('verdict') or ''),
                'report_title': str(extra.get('report_title') or ''),
                'quarantined': bool(extra.get('quarantined')),
            }
            if age_hours > stale_hours:
                errors.append(f'{name}: latest {kind} {age_hours:.0f}h old '
                              f'> {stale_hours:.0f}h')
        detail[name] = rows
    if not targets:
        warnings.append('no assessment target campaigns resolved')
    status = 'error' if errors else ('warning' if warnings else 'ok')
    if errors or warnings:
        summary = '; '.join(errors + warnings)
    else:
        summary = f'assessments fresh for {", ".join(targets)}'
    if notes:
        summary += ' (' + '; '.join(notes) + ')'
    data = {'targets': targets, 'campaigns': detail,
            'stale_hours': {'daily': daily_stale, 'weekly': weekly_stale}}
    return status, summary, data
