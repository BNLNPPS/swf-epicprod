"""Master production dashboard: the Ops tab of the production home page.

Panel providers for EPICPROD_DASHBOARD.md version 1. Each panel is a
bounded excerpt of an existing page — at most PANEL_LIMIT one-line
entries drawn from the same records the full page shows, plus links to
that page. Panels build independently: a provider failure surfaces as
an error line inside its panel rather than failing the page.
"""

import logging

from django.urls import reverse
from django.utils import timezone

logger = logging.getLogger(__name__)

PANEL_LIMIT = 8

# Default panel order: the production flow (EPICPROD_DASHBOARD.md).
PANEL_ORDER = (
    'requests', 'configs', 'campaigns', 'panda',
    'arrivals', 'assessments', 'proposals', 'live',
)


def _entry(text, when=None, url=''):
    return {'text': text, 'when': when, 'url': url}


def _fmt_bytes(value):
    value = float(value or 0)
    for unit in ('B', 'kB', 'MB', 'GB', 'TB', 'PB'):
        if value < 1000:
            return f'{value:,.1f} {unit}' if unit != 'B' else f'{int(value)} B'
        value /= 1000
    return f'{value:,.1f} EB'


def _panel_requests():
    from pcs.models import Questionnaire

    entries = []
    for row in Questionnaire.objects.order_by('-submitted_at')[:PANEL_LIMIT]:
        who = (row.contact or row.created_by or '').split('\n')[0]
        entries.append(_entry(
            f'{who}: {row.description}'.strip(': '),
            when=row.submitted_at,
            url=reverse('pcs:questionnaire_detail', args=[row.pk]),
        ))
    return {
        'id': 'requests', 'title': 'Requests', 'ai': False,
        'entries': entries,
        'links': [{'label': 'Request list',
                   'url': reverse('pcs:questionnaires_list')}],
        'empty': 'No requests recorded.',
    }


def _panel_configs():
    from pcs.models import ProdConfig

    entries = []
    for row in ProdConfig.objects.order_by('-updated_at')[:PANEL_LIMIT]:
        detail = row.jug_xl_tag or row.container_image or row.description
        text = f'{row.name} — {detail}' if detail else row.name
        entries.append(_entry(
            text, when=row.updated_at,
            url=reverse('pcs:prod_config_detail', args=[row.pk]),
        ))
    return {
        'id': 'configs', 'title': 'Physics configurations', 'ai': False,
        'entries': entries,
        'links': [{'label': 'Prod configs',
                   'url': reverse('pcs:prod_configs_list')}],
        'empty': 'No production configurations.',
    }


def _panel_campaigns():
    from pcs.models import Campaign
    from swf_epicprod.analytics.members import campaign_progress
    from swf_epicprod.analytics.rollup import producing_campaigns

    now = timezone.now()
    labelled = [(camp, 'producing') for camp, _ in producing_campaigns()]
    current = Campaign.objects.filter(lifecycle='current').first()
    if current and current.name not in {c.name for c, _ in labelled}:
        labelled.append((current, 'current'))
    labelled.sort(key=lambda pair: pair[0].name, reverse=True)

    entries = []
    catalog_url = reverse('pcs:pcs_catalog')
    for camp, label in labelled[:PANEL_LIMIT]:
        parts = [f'{camp.name} — {label}']
        block = campaign_progress(camp, now, now).get('data') or {}
        if block.get('available'):
            parts.append(f"{block.get('task_count', 0)} tasks, "
                         f"{block.get('tasks_with_processing', 0)} with processing")
            parts.append(f"{block.get('total_files', 0):,} files, "
                         f"{_fmt_bytes(block.get('total_bytes'))}")
            parts.append(f"{block.get('outputs_placement_complete', 0)}/"
                         f"{block.get('outputs_total', 0)} outputs placed")
        entries.append(_entry(' · '.join(parts), url=catalog_url))
    return {
        'id': 'campaigns', 'title': 'Campaigns', 'ai': False,
        'entries': entries,
        'links': [
            {'label': 'Catalog', 'url': catalog_url},
            {'label': 'Progress',
             'url': f'{catalog_url}?lifecycle=current&view=progress'},
        ],
        'empty': 'No current or producing campaign.',
    }


def _panel_panda():
    from monitor_app.panda.queries import get_activity

    activity = get_activity(days=1)
    jobs = activity.get('jobs') or {}
    tasks = activity.get('tasks') or {}
    entries = []

    by_status = jobs.get('by_status') or {}
    status_text = ', '.join(
        f'{status} {count:,}' for status, count in
        sorted(by_status.items(), key=lambda kv: -kv[1])[:4])
    entries.append(_entry(
        f"Jobs, 24 h: {jobs.get('total', 0):,}"
        + (f' — {status_text}' if status_text else '')))

    for row in (jobs.get('by_site') or [])[:3]:
        entries.append(_entry(
            f"{row.get('site')}: {int(row.get('total') or 0):,} jobs"))

    entries.append(_entry(f"Tasks, 24 h: {tasks.get('total', 0):,}"))
    return {
        'id': 'panda', 'title': 'PanDA activity', 'ai': False,
        'entries': entries,
        'links': [
            {'label': 'Activity', 'url': reverse('monitor_app:panda_activity')},
            {'label': 'Site usage', 'url': reverse('monitor_app:compute_usage')},
            {'label': 'Alarms', 'url': reverse('monitor_app:alarms_dashboard')},
        ],
        'empty': 'No PanDA activity in the window.',
    }


def _panel_arrivals():
    from monitor_app.models import AppLog

    rows = (
        AppLog.objects
        .filter(app_name='epicprod', extra_data__action='rucio_arrivals')
        .order_by('-timestamp')
        .values('timestamp', 'extra_data')[:20]
    )
    entries = []
    for row in rows:
        extra = row['extra_data'] if isinstance(row['extra_data'], dict) else {}
        for campaign, count in sorted((extra.get('campaigns') or {}).items()):
            if not count:
                continue
            entries.append(_entry(
                f'{campaign}: {int(count):,} new files at JLab',
                when=row['timestamp'],
            ))
            if len(entries) >= PANEL_LIMIT:
                break
        if len(entries) >= PANEL_LIMIT:
            break
    catalog_url = reverse('pcs:pcs_catalog')
    return {
        'id': 'arrivals', 'title': 'Science data arrivals', 'ai': False,
        'entries': entries,
        'links': [{'label': 'Campaign progress',
                   'url': f'{catalog_url}?lifecycle=current&view=progress'}],
        'empty': 'No arrivals in recent sweeps.',
    }


def _panel_assessments():
    from monitor_app.models import AppLog

    rows = (
        AppLog.objects
        .filter(app_name='epicprod',
                extra_data__action='assessment_register',
                extra_data__outcome='ok')
        .order_by('-timestamp')
        .values('timestamp', 'extra_data')[:50]
    )
    entries, seen = [], set()
    for row in rows:
        extra = row['extra_data'] if isinstance(row['extra_data'], dict) else {}
        subject = extra.get('subject_key') or ''
        if not subject or subject in seen:
            continue
        seen.add(subject)
        title = extra.get('report_title') or f'{subject} assessment'
        narration = str(extra.get('narration') or '').strip()
        text = f'{title} — {narration}' if narration else title
        entries.append(_entry(
            text, when=row['timestamp'], url=extra.get('report_path') or '',
        ))
        if len(entries) >= PANEL_LIMIT:
            break
    return {
        'id': 'assessments', 'title': 'AI assessments', 'ai': True,
        'entries': entries,
        'links': [{'label': 'All assessments',
                   'url': reverse('monitor_app:ai_content_list')}],
        'empty': 'No registered assessments.',
    }


def _panel_proposals():
    from ai.models import Proposal

    entries = []
    rows = (
        Proposal.objects
        .filter(status__in=('proposed', 'approved_pending_execution'))
        .order_by('-created_at')[:PANEL_LIMIT]
    )
    proposals_url = reverse('ai:ai_proposals')
    for row in rows:
        entries.append(_entry(
            f'{row.ref} {row.action} {row.subject_key} — {row.comment}',
            when=row.created_at, url=proposals_url,
        ))
    return {
        'id': 'proposals', 'title': 'AI proposals', 'ai': True,
        'entries': entries,
        'links': [{'label': 'Proposals', 'url': proposals_url}],
        'empty': 'No proposals awaiting decision.',
    }


def _panel_live():
    from monitor_app.epicprod_logging import EPICPROD_APP_NAME, live_stream_q
    from monitor_app.models import AppLog

    rows = (
        AppLog.objects
        .filter(app_name=EPICPROD_APP_NAME)
        .filter(live_stream_q())
        .order_by('-timestamp')[:PANEL_LIMIT]
    )
    entries = [
        _entry(row.message, when=row.timestamp) for row in rows
    ]
    return {
        'id': 'live', 'title': 'epicprod live', 'ai': False,
        'entries': entries,
        'links': [{'label': 'Live feed',
                   'url': f"{reverse('monitor_app:log_list')}?app_name=epicprod"}],
        'empty': 'No recent live actions.',
    }


_PROVIDERS = {
    'requests': _panel_requests,
    'configs': _panel_configs,
    'campaigns': _panel_campaigns,
    'panda': _panel_panda,
    'arrivals': _panel_arrivals,
    'assessments': _panel_assessments,
    'proposals': _panel_proposals,
    'live': _panel_live,
}


def build_dashboard():
    """Panels in default order. Every provider error is surfaced in place."""
    panels = []
    for panel_id in PANEL_ORDER:
        try:
            panels.append(_PROVIDERS[panel_id]())
        except Exception as exc:
            logger.exception('dashboard panel %s failed', panel_id)
            panels.append({
                'id': panel_id, 'title': panel_id, 'ai': False,
                'entries': [_entry(f'panel error: {exc}')],
                'links': [], 'empty': '',
            })
    return {'panels': panels}
