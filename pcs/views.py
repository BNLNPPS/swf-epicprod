"""
PCS web UI views and DataTable AJAX endpoints.

Views are generic across tag types (p/e/s/r) where possible, parameterized by tag_type.
Tag list views use server-side DataTables via monitor_app._datatable_base.html.
Read operations are public; create/edit/lock require login.
"""
import json
import logging
import re
import time
import hashlib
from functools import wraps
from zoneinfo import ZoneInfo

_ET = ZoneInfo('America/New_York')
from urllib.request import urlopen
from urllib.parse import quote as urlquote
from django.shortcuts import render, get_object_or_404, redirect
from django.template.loader import render_to_string
from django.contrib.auth.decorators import login_required
from django.urls import reverse
from django.http import JsonResponse, Http404
from django.contrib import messages
from django.core.cache import cache
from django.db.models import Count, Max, Q, Sum
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from monitor_app.models import UserPreference
from ai.assessments import ai_content_summary
from monitor_app.epicprod_logging import log_epicprod_action

# ---------------------------------------------------------------------------
# Auth / method-guard decorators that flash instead of silently redirecting.
# Project-wide NO-SILENT-FAILURES rule: an action-button click that hits a
# guard must tell the user what happened, never just refresh the page.
# ---------------------------------------------------------------------------

def _login_required_flash(view):
    """Like @login_required but flashes an explicit error before the redirect."""
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            messages.error(request, 'Sign in required for this action.')
            return redirect(f"{reverse('login')}?next={request.get_full_path()}")
        return view(request, *args, **kwargs)
    return wrapped


def _post_only_redirect(request, fallback_url, action_label='This action'):
    """Helper used by POST-only views: flash a warning, redirect to fallback.

    Use at the top of any POST-only handler instead of a bare
    ``if request.method != 'POST': return redirect(...)`` block.
    """
    messages.warning(request, f'{action_label} only responds to POST submissions.')
    return redirect(fallback_url)

from monitor_app.utils import DataTablesProcessor, get_filter_params, format_datetime

from .models import (
    PhysicsCategory, PhysicsTag, EvgenTag, SimuTag, RecoTag, BackgroundTag,
    Dataset, PhysicsConfig, ProdConfig, ProdTask,
    Campaign, Questionnaire, ProdRequest, EvgenMark,
    PRODTASK_STATUS_CHOICES, annotate_pwg_priority,
)
from .serializers import _redact_contact
from . import services

PROD_CONFIG_SCOUT_MODE_PREF = 'prod_config_scout_mode'


def _prod_config_scout_mode_pref(username):
    return bool(UserPreference.get_prefs(username).get(PROD_CONFIG_SCOUT_MODE_PREF, False))


CATALOG_TASK_LIST_CACHE_VERSION = 4
CATALOG_BUILD_TIMING_ENABLED = False

# The official ePIC physics working groups
# (https://www.epic-eic.org/physics/pwgs.html): the name, with the
# common acronym in parentheses where one exists. The option string is
# also the stored requestor value.
PWG_OPTIONS = (
    'Inclusive',
    'Semi-Inclusive',
    'Exclusive, Diffraction and Tagging (EDT)',
    'Jets and Heavy Flavour',
    'Beyond Standard Model and Electroweak (BSM & EW)',
)

# The official ePIC detector subsystem collaborations
# (https://www.epic-eic.org/detector/dsc.html), grouped for the request
# composer pulldown.
DSC_OPTION_GROUPS = (
    ('Particle Identification', (
        'dRICH',
        'hpDIRC',
        'Backwards RICH (pfRICH)',
    )),
    ('Tracking & Timing', (
        'Si Trackers (SVT)',
        'Gaseous Trackers (MPGD)',
        'AC-LGAD TOF',
    )),
    ('Calorimetry', (
        'Backwards ECAL (BECAL)',
        'Backwards HCAL (BHCAL)',
        'Barrel ECAL',
        'Barrel HCAL',
        'Forward ECAL',
        'Forward HCAL',
    )),
    ('Auxiliary & Beamline', (
        'Far-Forward (FF)',
        'Luminosity (Lumi)',
        'Far Backward High Rate Tracker (FB-HRT)',
    )),
)
DSC_OPTIONS = tuple(o for _, opts in DSC_OPTION_GROUPS for o in opts)

# Seed list of known requestor labels (PWGs + DSCs). Catalog pulldown
# surfaces these plus any distinct values already in the DB.
REQUESTOR_SEED_OPTIONS = PWG_OPTIONS + DSC_OPTIONS


def _timing_ms(seconds):
    return round(seconds * 1000.0, 1)


def _timing_record(timings, label, start, *, detail=''):
    if timings is not None:
        ms = _timing_ms(time.perf_counter() - start)
        timings.append({
            'label': label,
            'ms': ms,
            'ms_display': f'{ms} ms',
            'detail': detail,
        })


def _timing_note(timings, label, *, detail=''):
    if timings is not None:
        timings.append({'label': label, 'ms': None, 'ms_display': '', 'detail': detail})


def _timed(timings, label, fn, *, detail_fn=None):
    if timings is None:
        return fn()
    start = time.perf_counter()
    result = fn()
    detail = detail_fn(result) if detail_fn else ''
    _timing_record(timings, label, start, detail=detail)
    return result


def _requestor_options():
    """Distinct existing requestors ∪ seed options, sorted."""
    cache_key = 'pcs:catalog:requestor-options:v1'
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    seen = set(REQUESTOR_SEED_OPTIONS)
    seen.update(
        ProdRequest.objects.exclude(requestor='')
        .values_list('requestor', flat=True).distinct()
    )
    seen.update(
        ProdTask.objects.exclude(requestor='')
        .values_list('requestor', flat=True).distinct()
    )
    options = sorted(seen)
    cache.set(cache_key, options, 300)
    return options


def _generator_display_case():
    """Map lowercased generator identities to the proper-case spelling
    recorded on evgen tags ('beagle' -> 'BeAGLE'). Physics-configuration
    keys lowercase the generator for stable comparison
    (pcs/physics_config.py); displays map back through this."""
    return {
        str(g).lower(): str(g)
        for g in EvgenTag.objects.values_list('parameters__generator',
                                              flat=True)
        if g
    }


def _parse_catalog_filters(request):
    """Parse catalog filter query params into a dict of clean values."""
    return {
        'q': (request.GET.get('q') or '').strip(),
        'status': (request.GET.get('status') or '').strip(),
        'requestor': (request.GET.get('requestor') or '').strip(),
        'submission_path': (request.GET.get('submission_path') or '').strip(),
        'pre_tdr_use': request.GET.get('pre_tdr_use') == '1',
        'early_science_use': request.GET.get('early_science_use') == '1',
        'other_use': request.GET.get('other_use') == '1',
    }


def _apply_catalog_filters(qs, filters):
    """Apply a parsed-filters dict to a ProdTask queryset."""
    if filters['q']:
        qs = qs.filter(Q(name__icontains=filters['q'])
                       | Q(description__icontains=filters['q']))
    if filters['status']:
        qs = qs.filter(status=filters['status'])
    if filters['requestor']:
        qs = qs.filter(requestor=filters['requestor'])
    if filters['submission_path']:
        # submission_path lives in ProdConfig.data JSON; default 'condor'
        if filters['submission_path'] == 'condor':
            # Match rows where data is null/missing the key (default) OR key='condor'
            qs = qs.filter(
                Q(prod_config__data__submission_path='condor')
                | Q(prod_config__data__submission_path__isnull=True)
                | Q(prod_config__data__isnull=True)
            )
        else:
            qs = qs.filter(prod_config__data__submission_path=filters['submission_path'])
    if filters['pre_tdr_use']:
        qs = qs.filter(pre_tdr_use=True)
    if filters['early_science_use']:
        qs = qs.filter(early_science_use=True)
    if filters['other_use']:
        qs = qs.filter(other_use=True)
    return qs


def _catalog_view_url(request, active_lifecycle, view_mode):
    q = request.GET.copy()
    q['lifecycle'] = active_lifecycle
    if view_mode == 'progress':
        q['view'] = 'progress'
    else:
        q.pop('view', None)
    q.pop('refresh', None)
    encoded = q.urlencode()
    return '?' + encoded if encoded else '?'


def _annotate_task_progress(tasks, snapshot):
    rows = (snapshot or {}).get('rows') or {}
    empty = {'outputs': [], 'configured_jobs': None, 'has_processing': False}
    empty_processing = {
        'jeditaskid': '', 'status': '', 'total_jobs': '', 'nfailed': '',
        'nactive': '', 'nfinished': '', 'nfinalfailed': '',
        'processing_percent': None, 'final_failure_rate': None,
    }
    empty_output = {
        'completion_percent': None, 'expected_jobs': '', 'link': '',
        'processing': empty_processing,
    }
    for task in tasks:
        task.progress = rows.get(str(task.pk), empty)
        outputs = task.progress.get('outputs') or []
        if outputs:
            first = dict(empty_output)
            first.update(outputs[0])
            processing = dict(empty_processing)
            processing.update(first.get('processing') or {})
            first['processing'] = processing
            task.progress_first = first
        else:
            task.progress_first = empty_output
        linked = []
        completion_values = []
        job_values = []
        for output in outputs:
            if output.get('completion_percent') is not None:
                completion_values.append(output.get('completion_percent'))
            processing = output.get('processing') or {}
            total_jobs = processing.get('total_jobs') or output.get('expected_jobs')
            if total_jobs not in (None, ''):
                try:
                    job_values.append(int(total_jobs))
                except (TypeError, ValueError):
                    pass
            if processing.get('jeditaskid'):
                linked.append(output)
        failure_values = []
        for output in linked:
            try:
                failure_values.append(int((output.get('processing') or {}).get('nfailed') or 0))
            except (TypeError, ValueError):
                failure_values.append(0)
        task.progress_sort = {
            'completion': max(completion_values) if completion_values else -1,
            'jobs': max(job_values) if job_values else '',
            'processing': (
                '1:' + str((linked[0].get('processing') or {}).get('status') or '')
                if linked else '0:'
            ),
            'failures': (
                f'1:{max(failure_values):09d}' if failure_values else '0:'
            ),
            'link': '1:' + str(linked[0].get('link') or '') if linked else '0:',
        }
    return tasks


def _catalog_cache_dt(value):
    return value.isoformat() if value else ''


def _catalog_task_list_cache_signature(campaign, catalog_view, progress_snapshot):
    from ai.models import Proposal

    task_meta = ProdTask.objects.filter(campaign=campaign).aggregate(
        count=Count('id'), updated=Max('updated_at'))
    # AI proposal activity changes the rendered rows (pending badges and
    # filters, executed marks) without touching any ProdTask, so it is
    # part of the signature: creation adds rows, decide stamps
    # decided_at, undo stamps undone_at.
    proposal_meta = Proposal.objects.aggregate(
        count=Count('id'), created=Max('created_at'),
        decided=Max('decided_at'), undone=Max('undone_at'))
    return {
        'version': CATALOG_TASK_LIST_CACHE_VERSION,
        'view': catalog_view,
        'campaign_id': campaign.pk,
        'campaign_name': campaign.name,
        'task_count': task_meta['count'] or 0,
        'task_updated_at': _catalog_cache_dt(task_meta['updated']),
        # A PWG priority change re-renders the table (the PWG column).
        'pwg_priority_set_at': _catalog_cache_dt(
            EvgenMark.objects.aggregate(m=Max('priority_set_at'))['m']),
        'proposal_count': proposal_meta['count'] or 0,
        'proposal_created_at': _catalog_cache_dt(proposal_meta['created']),
        'proposal_decided_at': _catalog_cache_dt(proposal_meta['decided']),
        'proposal_undone_at': _catalog_cache_dt(proposal_meta['undone']),
        'progress_generated_at': (
            (progress_snapshot or {}).get('generated_at') or ''
            if catalog_view == 'progress' else ''
        ),
    }


def _executed_proposal_names():
    """Composed names carrying an executed AI proposal — the catalog's
    'AI: executed' filter reads this (pending proposals ride the render
    projection instead)."""
    from ai.models import Proposal
    return set(Proposal.objects.filter(status='executed')
               .values_list('subject_key', flat=True))


def _version_tuple(name):
    """Campaign version as a comparable tuple ('26.4.1' < '26.7'), or
    None when the name is not a dotted-integer version."""
    try:
        return tuple(int(p) for p in str(name or '').split('.'))
    except ValueError:
        return None


def _promotion_forward(candidate_name, current_name):
    """Rotation must move forward: a producing campaign earns its
    promote/populate affordances only when strictly newer than current
    (or when no current campaign exists). An older campaign still
    producing beside current — the 26.06 long tail — gets neither."""
    candidate = _version_tuple(candidate_name)
    if not candidate:
        return False
    if not current_name:
        return True
    current = _version_tuple(current_name)
    return bool(current and candidate > current)


def _next_campaign_hint():
    """The likely next campaign, derived from pending campaign-propagation
    proposal batches named '<next-campaign>-dispositions-<date>' (PCS.md).
    Returns {'name', 'pending', 'batches'} for the newest such version, or
    None when no batch names one."""
    import re

    from ai.models import Proposal
    hints = {}
    pending = (Proposal.objects
               .filter(action='propagation', status='proposed')
               .exclude(batch_id='')
               .values_list('batch_id')
               .annotate(Count('id')).order_by())
    for batch_id, count in pending:
        m = re.match(r'^(\d+(?:\.\d+)*)-dispositions-', batch_id)
        if not m or _version_tuple(m.group(1)) is None:
            continue
        hint = hints.setdefault(m.group(1), {'pending': 0, 'batches': []})
        hint['pending'] += count
        hint['batches'].append(batch_id)
    if not hints:
        return None
    # A hint is only "next" while its family is beyond the current
    # campaign: once that campaign exists and is current, a leftover
    # pending-proposal batch must not resurrect it on the Future tab.
    from .name_tokens import campaign_family
    current = Campaign.objects.filter(lifecycle='current').first()
    if current:
        current_key = _version_tuple(campaign_family(current.name))
        if current_key:
            hints = {
                name: hint for name, hint in hints.items()
                if (_version_tuple(campaign_family(name)) or (0,)) > current_key
            }
    if not hints:
        return None
    name = max(hints, key=_version_tuple)
    return {'name': name, **hints[name]}


def _campaign_last_activity(campaign):
    """The most recent Rucio arrival known for a campaign, as an ISO
    string ready for the ``fmt_dt`` filter (Eastern display everywhere):
    the arrivals sweep's record when present, else the last cumulative
    increase in the campaign's Rucio timeline (kept for current/last
    campaigns), else ''."""
    arrivals = (campaign.data or {}).get('arrivals') or {}
    value = arrivals.get('last_arrival_at') or ''
    if value:
        return value
    from .services import load_rucio_timeline
    timeline = load_rucio_timeline(campaign.name) or {}
    dates = timeline.get('dates') or []
    best = ''
    for key in ('reco', 'simu'):
        cum = (timeline.get(key) or {}).get('cum_files') or []
        for i in range(1, min(len(cum), len(dates))):
            if cum[i] > cum[i - 1] and dates[i] > best:
                best = dates[i]
    # Timeline bins are naive UTC; stamp the zone so display converts.
    return (best + '+00:00') if best else ''


def _promote_cascade_note(campaigns_by_lifecycle, target_name):
    """Human line stating what the promote rotation will do."""
    steps = [f'{c.name} becomes last'
             for c in campaigns_by_lifecycle['current']
             if c.name != target_name]
    steps += [f'{c.name} becomes past'
              for c in campaigns_by_lifecycle['last']
              if c.name != target_name]
    return ('; '.join(steps) + '.') if steps else ''


def _campaigns_with_inflow():
    """Campaigns with fresh Rucio arrivals — the derived 'producing'
    status. Single source: swf_epicprod.analytics.rollup.producing_campaigns
    (also behind the campaign status rollup and the assessment trigger).
    """
    from swf_epicprod.analytics.rollup import producing_campaigns
    return producing_campaigns()


def _catalog_table_cache_key(campaign_id, catalog_view, signature):
    payload = json.dumps(signature, sort_keys=True, separators=(',', ':'))
    digest = hashlib.sha256(payload.encode('utf-8')).hexdigest()
    return f'pcs:catalog-table:{campaign_id}:{catalog_view}:{digest}'


def _catalog_table_latest_key(campaign_id, catalog_view):
    return f'pcs:catalog-table:latest:{campaign_id}:{catalog_view}'


def _campaign_data(campaign):
    if campaign is None:
        return {}
    return (
        Campaign.objects
        .filter(pk=campaign.pk)
        .values_list('data', flat=True)
        .first()
    ) or {}


def _current_catalog_tasks(campaign, catalog_view, progress_snapshot, timings=None):
    def load_tasks():
        return list(
            ProdTask.objects.select_related(
                'campaign', 'dataset', 'prod_config', 'request',
                'dataset__physics_tag', 'dataset__evgen_tag', 'dataset__simu_tag',
                'dataset__reco_tag', 'dataset__background_tag',
            ).filter(campaign=campaign).order_by('-updated_at')
        )
    tasks = _timed(
        timings,
        'task query',
        load_tasks,
        detail_fn=lambda rows: f'{len(rows)} rows',
    )
    tasks = _timed(
        timings,
        'questionnaire match cache hydrate',
        lambda: _annotate_task_questionnaire_matches(tasks),
        detail_fn=lambda rows: f'{len(rows)} task-local cached match lists',
    )
    tasks = _timed(
        timings,
        'pc request projection',
        lambda: _annotate_task_pc_requests(tasks),
        detail_fn=lambda rows: f'{len(rows)} rows',
    )
    tasks = _timed(
        timings,
        'pwg priority preload',
        lambda: annotate_pwg_priority(tasks),
        detail_fn=lambda rows: f'{len(rows)} rows',
    )
    if catalog_view == 'progress':
        tasks = _timed(
            timings,
            'progress row annotation',
            lambda: _annotate_task_progress(tasks, progress_snapshot),
            detail_fn=lambda rows: f'{len(rows)} rows',
        )
    return tasks


def _cached_current_task_list_html(campaign, catalog_view, context,
                                   progress_snapshot, timings=None,
                                   rebuild_on_miss=False):
    """``rebuild_on_miss`` skips the stale-serve suppression: low-traffic
    views (past/last/producing tabs) accept the inline rebuild cost
    rather than serving a stale table with no other rebuilder."""
    if campaign is None or catalog_view not in ('catalog', 'progress'):
        return None, False, {}
    signature = _timed(
        timings,
        'table cache signature',
        lambda: _catalog_task_list_cache_signature(campaign, catalog_view, progress_snapshot),
    )
    cache_key = _catalog_table_cache_key(campaign.pk, catalog_view, signature)
    latest_key = _catalog_table_latest_key(campaign.pk, catalog_view)
    cached = _timed(
        timings,
        'table cache lookup',
        lambda: cache.get(cache_key),
        detail_fn=lambda value: (
            f'cache hit, {len(value.get("html", ""))} html bytes'
            if value and value.get('html') else 'miss'
        ),
    )
    if cached and cached.get('html'):
        cache.set(latest_key, cache_key, None)
        _timing_note(
            timings,
            'table render',
            detail='Django cache hit',
        )
        return cached['html'], True, cached

    latest_cache_key = cache.get(latest_key)
    if rebuild_on_miss:
        latest_cache_key = None
    if latest_cache_key and latest_cache_key != cache_key:
        stale = _timed(
            timings,
            'table stale cache lookup',
            lambda: cache.get(latest_cache_key),
            detail_fn=lambda value: (
                f'stale cache hit, {len(value.get("html", ""))} html bytes'
                if value and value.get('html') else 'miss'
            ),
        )
        if stale and stale.get('html'):
            _timing_note(
                timings,
                'table render',
                detail='stale Django cache used; page-load rebuild suppressed',
            )
            return stale['html'], True, {**stale, 'stale': True}

    tasks = _timed(
        timings,
        'table cache miss task query',
        lambda: _current_catalog_tasks(campaign, catalog_view, progress_snapshot),
        detail_fn=lambda rows: f'{len(rows)} rows',
    )
    html = _timed(
        timings,
        'table cache miss render',
        lambda: render_to_string(
            'pcs/_task_list_filter.html',
            {
                'tasks': tasks,
                'catalog_view': catalog_view,
                'columns_mode': 'full',
                'status_choices': PRODTASK_STATUS_CHOICES,
                'ai_executed_names': _executed_proposal_names(),
            },
        ),
        detail_fn=lambda value: f'{len(value)} html bytes',
    )
    entry = {
        'signature': signature,
        'html': html,
        'rendered_at': timezone.now().isoformat(),
    }
    cache.set(cache_key, entry, None)
    cache.set(latest_key, cache_key, None)
    _timing_note(
        timings,
        'table render',
        detail='cache miss rebuilt and cached',
    )
    return html, False, entry


def rebuild_current_task_list_html_cache(campaign, catalog_view='catalog', progress_snapshot=None):
    """Rebuild the current-campaign table fragment outside the page GET path."""
    if campaign is None or catalog_view not in ('catalog', 'progress'):
        raise ValueError('campaign and catalog/progress view are required')
    signature = _catalog_task_list_cache_signature(campaign, catalog_view, progress_snapshot)
    tasks = _current_catalog_tasks(campaign, catalog_view, progress_snapshot)
    html = render_to_string(
        'pcs/_task_list_filter.html',
        {
            'tasks': tasks,
            'catalog_view': catalog_view,
            'columns_mode': 'full',
            'status_choices': PRODTASK_STATUS_CHOICES,
            'ai_executed_names': _executed_proposal_names(),
        },
    )
    entry = {
        'signature': signature,
        'html': html,
        'rendered_at': timezone.now().isoformat(),
    }
    cache_key = _catalog_table_cache_key(campaign.pk, catalog_view, signature)
    cache.set(cache_key, entry, None)
    cache.set(_catalog_table_latest_key(campaign.pk, catalog_view), cache_key, None)
    return {
        'campaign': campaign.name,
        'view': catalog_view,
        'tasks': len(tasks),
        'html_bytes': len(html),
        'rendered_at': entry['rendered_at'],
    }


from .schemas import TAG_SCHEMAS, get_tag_model, get_param_defs, save_param_defs
from .forms import PhysicsTagForm, SimpleTagForm, DatasetForm, PhysicsCategoryForm, ProdConfigForm


def pcs_hub_counts():
    """PCS entity counts — shared by PCS hub and production hub."""
    return {
        'categories_count': PhysicsCategory.objects.count(),
        'physics_tags_count': PhysicsTag.objects.count(),
        'evgen_tags_count': EvgenTag.objects.count(),
        'simu_tags_count': SimuTag.objects.count(),
        'reco_tags_count': RecoTag.objects.count(),
        'background_tags_count': BackgroundTag.objects.count(),
        'datasets_count': Dataset.objects.values('dataset_name').distinct().count(),
        'questionnaires_count': Questionnaire.objects.count(),
        'prod_configs_count': ProdConfig.objects.count(),
        'prod_tasks_count': ProdTask.objects.count(),
    }


def pcs_hub(request):
    return render(request, 'pcs/pcs_hub.html', pcs_hub_counts())


# ── Questionnaire intake ───────────────────────────────────────────

def _questionnaire_contact_display(questionnaire, *, authenticated):
    if not authenticated:
        return _redact_contact(questionnaire.contact)

    contacts = (questionnaire.data or {}).get('contacts') or []
    parts = []
    for contact in contacts:
        if not isinstance(contact, dict):
            continue
        name = (contact.get('name') or '').strip()
        emails = [
            str(email).strip()
            for email in (contact.get('emails') or [])
            if str(email).strip()
        ]
        if name and emails:
            parts.append(f"{name} ({', '.join(emails)})")
        elif name:
            parts.append(name)
        elif emails:
            parts.append(', '.join(emails))
    return ', '.join(parts) or questionnaire.contact


def _questionnaire_contacts(questionnaire):
    return [
        contact for contact in ((questionnaire.data or {}).get('contacts') or [])
        if isinstance(contact, dict)
    ]


def _questionnaire_contact_names(questionnaire):
    names = []
    seen = set()
    for contact in _questionnaire_contacts(questionnaire):
        name = (contact.get('name') or '').strip()
        key = name.lower()
        if name and key not in seen:
            seen.add(key)
            names.append(name)
    return names


def _questionnaire_has_email(questionnaire):
    return any(contact.get('emails') for contact in _questionnaire_contacts(questionnaire))


def _questionnaire_data_label(questionnaire, key):
    value = (questionnaire.data or {}).get(key) or {}
    return (value.get('label') or '').strip() if isinstance(value, dict) else ''


def _questionnaire_prod_matches(questionnaire, *, status=None):
    matches = []
    for match in (questionnaire.data or {}).get('prod_matches') or []:
        if not isinstance(match, dict):
            continue
        if status and (match.get('status') or 'accepted') != status:
            continue
        match = dict(match)
        matched_at = match.get('matched_at')
        if matched_at:
            dt = parse_datetime(str(matched_at))
            match['matched_at_display'] = format_datetime(dt) if dt else matched_at
        else:
            match['matched_at_display'] = ''
        matches.append(match)
    return matches


def _task_display_name(task):
    return task.composed_name or task.name


def _resolve_questionnaire_match_task(match):
    from .services import resolve_prodtask
    qs = ProdTask.objects.select_related('campaign', 'dataset', 'prod_config')
    for key in (match.get('task_name'), match.get('legacy_name'), match.get('task_id')):
        if not key:
            continue
        try:
            return resolve_prodtask(str(key), qs)
        except ProdTask.DoesNotExist:
            continue
    return None


def _annotate_questionnaire_matches(questionnaires):
    for questionnaire in questionnaires:
        matches = _questionnaire_prod_matches(questionnaire, status='accepted')
        questionnaire.prod_match_count = len(matches)
        questionnaire.prod_matches = matches


def _instancing_context(source_name, target_name):
    """The instancing box context — the recomputed plan, the last
    population run, the class layout — shared by the producing and
    future tabs (CAMPAIGN_CONTINUUM.md). The plan tolerates a target
    campaign with no row yet (the batch-derived next campaign): every
    continuing configuration classifies as mint, so the box lays out
    what a population would do before the campaign exists."""
    from monitor_app.models import AppLog

    from .instancing import plan_campaign_instancing
    plan = plan_campaign_instancing(source_name, target_name)
    last_run_row = (AppLog.objects
                    .filter(app_name='epicprod',
                            extra_data__action='campaign_instancing',
                            extra_data__subject_key=target_name)
                    .order_by('-timestamp').first())
    last_run = None
    if last_run_row is not None:
        message = last_run_row.message or ''
        last_run = {
            'at': last_run_row.timestamp,
            'by': (last_run_row.extra_data or {}).get('username', ''),
            'outcome': (last_run_row.extra_data or {}).get('outcome', ''),
            'summary': message.split(': ', 1)[-1],
        }
    return {
        'source': source_name,
        'target': target_name,
        'last_run': last_run,
        'plan': plan,
        'classes': [
            ('Adopt (already produced)', 'merge', plan['merge']),
            ('Mint (planned, not yet produced)', 'mint',
             plan['mint']),
            ('Aligned (already instanced, nothing to do)',
             'aligned', plan['aligned']),
            ('No request context — curation supplies it',
             'no_context', plan['no_context']),
            ('Name collision — anchor-tag ambiguity, curation',
             'name_collision', plan['name_collision']),
            ('Hold', 'hold', plan['hold']),
            ('Final', 'final', plan['final']),
            ('Unresolved — curation pool', 'unresolved',
             plan['unresolved']),
            ('Conflicting dispositions', 'conflict',
             plan['conflict']),
            ('Only in this campaign', 'target_only',
             plan['target_only']),
        ],
    }


def _annotate_task_pc_requests(tasks):
    """Attach PC-projected production requests as ``task.pc_requests``:
    a task points to requests through its physics configuration
    (CAMPAIGN_CONTINUUM.md), never a per-task binding."""
    from .services import pc_request_projection
    projection = pc_request_projection(
        [t.dataset for t in tasks if t.dataset_id])
    for task in tasks:
        task.pc_requests = (projection.get(task.dataset.composed_name, [])
                            if task.dataset_id else [])
    return tasks


def _annotate_task_questionnaire_matches(tasks):
    tasks = list(tasks)
    qids = set()
    for task in tasks:
        task.questionnaire_matches = []
        for match in (task.overrides or {}).get('questionnaire_matches') or []:
            if not isinstance(match, dict):
                continue
            qid = match.get('questionnaire_id')
            if isinstance(qid, int) or str(qid).isdigit():
                qids.add(int(qid))
    questionnaires = {
        q.pk: q for q in Questionnaire.objects.filter(pk__in=qids)
    } if qids else {}
    for task in tasks:
        for match in (task.overrides or {}).get('questionnaire_matches') or []:
            if not isinstance(match, dict):
                continue
            qid = match.get('questionnaire_id')
            questionnaire = questionnaires.get(int(qid)) if str(qid).isdigit() else None
            if questionnaire is None:
                continue
            task.questionnaire_matches.append({
                'questionnaire': questionnaire,
                'confidence': match.get('confidence') or '',
                'reason': match.get('reason') or '',
            })
    return tasks


def questionnaires_list(request):
    rows = list(Questionnaire.objects.all())
    authenticated = request.user.is_authenticated
    for row in rows:
        row.contact_display = _questionnaire_contact_display(
            row, authenticated=authenticated)
        row.repository_display = _questionnaire_data_label(
            row, 'repository_curated')
        row.generator_display = _questionnaire_data_label(row, 'generator')
        row.generator_filter = row.generator_display or '__undefined__'
        row.has_contact = bool(_questionnaire_contacts(row))
        row.has_email = _questionnaire_has_email(row)
        row.contact_filter = '||'.join(_questionnaire_contact_names(row))
        row.search_text = ' '.join([
            row.description or '',
            row.repository or '',
            row.repository_display or '',
            row.generator_display or '',
            row.contact_display or '',
            row.nevents or '',
            row.benchmark or '',
            row.estimate or '',
        ]).lower()
    _annotate_questionnaire_matches(rows)
    return render(request, 'pcs/questionnaires_list.html', {
        'questionnaires': rows,
        'total_count': len(rows),
    })


def questionnaire_detail(request, pk):
    questionnaire = get_object_or_404(Questionnaire, pk=pk)
    authenticated = request.user.is_authenticated
    questionnaire.contact_display = _questionnaire_contact_display(
        questionnaire, authenticated=authenticated)
    questionnaire.repository_display = _questionnaire_data_label(
        questionnaire, 'repository_curated')
    questionnaire.generator_display = _questionnaire_data_label(
        questionnaire, 'generator')
    matches = []
    for match in _questionnaire_prod_matches(questionnaire):
        resolved = _resolve_questionnaire_match_task(match)
        matches.append({'match': match, 'task': resolved})
    return render(request, 'pcs/questionnaire_detail.html', {
        'questionnaire': questionnaire,
        'matches': matches,
        'confidence_choices': ('high', 'medium', 'low'),
    })


@_login_required_flash
def questionnaire_match_add(request, pk):
    questionnaire = get_object_or_404(Questionnaire, pk=pk)
    if request.method != 'POST':
        return _post_only_redirect(
            request, reverse('pcs:questionnaire_detail', kwargs={'pk': pk}),
            action_label='Questionnaire match')
    task_key = (request.POST.get('task') or '').strip()
    confidence = (request.POST.get('confidence') or '').strip()
    reason = (request.POST.get('reason') or '').strip()
    if confidence not in {'high', 'medium', 'low'}:
        confidence = 'medium'
    if not task_key:
        messages.error(request, 'Provide a production task name.')
        return redirect('pcs:questionnaire_detail', pk=pk)
    from .services import resolve_prodtask
    try:
        task = resolve_prodtask(
            task_key, ProdTask.objects.select_related('dataset', 'campaign'))
    except ProdTask.DoesNotExist:
        messages.error(request, f'No production task matches {task_key!r}.')
        return redirect('pcs:questionnaire_detail', pk=pk)

    data = dict(questionnaire.data or {})
    matches = [
        match for match in (data.get('prod_matches') or [])
        if isinstance(match, dict) and match.get('task_id') != task.pk
    ]
    matches.append({
        'task_id': task.pk,
        'task_name': _task_display_name(task),
        'legacy_name': task.name,
        # The match binds to the physics configuration through this
        # name reference; the cache rebuild projects it onto every
        # edition of the same physics (CAMPAIGN_CONTINUUM.md).
        'pc_anchor': task.composed_name if task.dataset_id else '',
        'confidence': confidence,
        'status': 'accepted',
        'reason': reason,
        'matched_by': getattr(request.user, 'username', '') or 'web',
        'matched_at': timezone.now().isoformat(),
    })
    data['prod_matches'] = matches
    questionnaire.data = data
    questionnaire.save(update_fields=['data', 'updated_at'])
    messages.success(request, f'Matched request #{pk} to {_task_display_name(task)}.')
    log_epicprod_action(
        'web', 'questionnaire_match_add',
        subject_type='campaign_task', subject_key=task.composed_name,
        username=getattr(request.user, 'username', ''),
        sublevel='normal', live_default=True, questionnaire=pk)
    return redirect('pcs:questionnaire_detail', pk=pk)


@_login_required_flash
def questionnaire_match_remove(request, pk, task_id):
    questionnaire = get_object_or_404(Questionnaire, pk=pk)
    if request.method != 'POST':
        return _post_only_redirect(
            request, reverse('pcs:questionnaire_detail', kwargs={'pk': pk}),
            action_label='Questionnaire match removal')
    data = dict(questionnaire.data or {})
    before = len(data.get('prod_matches') or [])
    data['prod_matches'] = [
        match for match in (data.get('prod_matches') or [])
        if not (isinstance(match, dict) and str(match.get('task_id')) == str(task_id))
    ]
    questionnaire.data = data
    questionnaire.save(update_fields=['data', 'updated_at'])
    removed = before - len(data['prod_matches'])
    if removed:
        messages.success(request, f'Removed {removed} production match.')
        log_epicprod_action(
            'web', 'questionnaire_match_remove',
            username=getattr(request.user, 'username', ''),
            sublevel='normal', live_default=True,
            questionnaire=pk, task_id=task_id)
    else:
        messages.warning(request, 'No matching production task link was present.')
    return redirect('pcs:questionnaire_detail', pk=pk)


@_login_required_flash
def questionnaire_import(request):
    if request.method != 'POST':
        return _post_only_redirect(
            request, reverse('pcs:questionnaires_list'),
            action_label='Questionnaire import')
    from .services import questionnaire_intake_csv, ServiceError
    csv_url = (request.POST.get('csv_url') or '').strip()
    if not csv_url:
        messages.error(request, 'Provide a questionnaire CSV import URL.')
        return redirect(reverse('pcs:questionnaires_list'))
    try:
        with urlopen(csv_url, timeout=30) as response:
            csv_text = response.read().decode('utf-8-sig')
    except Exception as e:
        messages.error(request, f'Questionnaire CSV fetch failed: {e}')
        return redirect(reverse('pcs:questionnaires_list'))
    if not csv_text.strip():
        messages.error(request, 'Questionnaire CSV import URL returned no CSV text.')
        return redirect(reverse('pcs:questionnaires_list'))
    try:
        summary = questionnaire_intake_csv(
            csv_text,
            source_url=csv_url,
            created_by=getattr(request.user, 'username', '') or 'questionnaire_import',
        )
    except ServiceError as e:
        messages.error(request, f'Questionnaire import failed: {e.detail}')
        return redirect(reverse('pcs:questionnaires_list'))
    messages.success(
        request,
        f'Questionnaire import: {summary["created"]} new, '
        f'{summary["updated"]} updated, {summary["unchanged"]} unchanged.'
    )
    log_epicprod_action(
        'web', 'questionnaire_import',
        username=getattr(request.user, 'username', ''),
        sublevel='normal', live_default=True,
        created=summary['created'], updated=summary['updated'],
        unchanged=summary['unchanged'])
    return redirect(reverse('pcs:questionnaires_list'))


# ── Physics Categories ────────────────────────────────────────────

def physics_categories_list(request):
    categories = PhysicsCategory.objects.annotate(tag_count=Count('tags')).order_by('digit')
    return render(request, 'pcs/physics_categories_list.html', {'categories': categories})


@_login_required_flash
def physics_category_create(request):
    if request.method == 'POST':
        form = PhysicsCategoryForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, f"Category {form.instance.digit}: {form.instance.name} created.")
            log_epicprod_action(
                'web', 'category_create',
                subject_key=f'{form.instance.digit} {form.instance.name}',
                username=getattr(request.user, 'username', ''),
                sublevel='normal', live_default=True)
            return redirect('pcs:physics_categories_list')
    else:
        form = PhysicsCategoryForm()
    return render(request, 'pcs/physics_category_create.html', {'form': form})


# ── Tag list/detail/create (generic across p/e/s/r) ──────────────

TAG_MODELS = {
    'p': PhysicsTag,
    'e': EvgenTag,
    's': SimuTag,
    'r': RecoTag,
    'k': BackgroundTag,
}


def _tag_schema_or_404(tag_type):
    """An unknown tag type in the URL is a 404, never a server error."""
    if tag_type not in TAG_SCHEMAS:
        raise Http404(f'Unknown tag type {tag_type!r}')
    return TAG_SCHEMAS[tag_type]


def tags_list(request, tag_type):
    schema = _tag_schema_or_404(tag_type)
    model = TAG_MODELS[tag_type]

    status_filter = request.GET.get('status', '')
    category_filter = request.GET.get('category', '')

    columns = [
        {'name': 'tag_label', 'title': 'Tag', 'orderable': True},
        {'name': 'description', 'title': 'Description', 'orderable': True},
        {'name': 'status', 'title': 'Status', 'orderable': True},
        {'name': 'created_by', 'title': 'Created By', 'orderable': True},
        {'name': 'created_at', 'title': 'Created', 'orderable': True},
        {'name': 'actions', 'title': '', 'orderable': False},
    ]
    if tag_type == 'p':
        columns.insert(1, {'name': 'category__name', 'title': 'Category', 'orderable': True})

    statuses = ['draft', 'locked']
    categories = list(PhysicsCategory.objects.values_list('name', flat=True)) if tag_type == 'p' else []

    context = {
        'table_title': f'{schema["label"]} Tags',
        'table_description': f'All {schema["label"].lower()} tags registered in PCS.',
        'ajax_url': reverse('pcs:tags_datatable_ajax', args=[tag_type]),
        'columns': columns,
        'tag_type': tag_type,
        'schema': schema,
        'statuses': statuses,
        'categories': categories,
        'selected_status': status_filter,
        'selected_category': category_filter,
    }
    return render(request, 'pcs/tag_list.html', context)


def tags_datatable_ajax(request, tag_type):
    model = TAG_MODELS[tag_type]

    if tag_type == 'p':
        col_names = ['tag_label', 'category__name', 'description', 'status', 'created_by', 'created_at', 'actions']
    else:
        col_names = ['tag_label', 'description', 'status', 'created_by', 'created_at', 'actions']

    dt = DataTablesProcessor(request, col_names, default_order_column=0, default_order_direction='desc')

    qs = model.objects.all()
    if tag_type == 'p':
        qs = qs.select_related('category')

    filters = get_filter_params(request, ['status', 'category'])
    if filters['status']:
        qs = qs.filter(status=filters['status'])
    if tag_type == 'p' and filters.get('category'):
        qs = qs.filter(category__name=filters['category'])

    records_total = model.objects.count()
    search_fields = ['tag_label', 'description', 'created_by']
    if tag_type == 'p':
        search_fields.append('category__name')
    qs = dt.apply_search(qs, search_fields)
    records_filtered = qs.count()

    qs = qs.order_by(dt.get_order_by())
    page = dt.apply_pagination(qs)

    data = []
    for tag in page:
        compose_url = reverse('pcs:tag_compose', args=[tag_type])
        tag_url = f'{compose_url}?selected={urlquote(tag.tag_label)}'
        tag_link = f'<a href="{tag_url}">{tag.tag_label}</a>'
        status_badge = (
            f'<span class="badge bg-secondary">{tag.status}</span>'
            if tag.status == 'draft'
            else f'<span class="badge bg-success">{tag.status}</span>'
        )
        row = [tag_link]
        if tag_type == 'p':
            row.append(tag.category.name)
        row += [
            tag.description[:80] + ('...' if len(tag.description) > 80 else ''),
            status_badge,
            tag.created_by,
            format_datetime(tag.created_at),
            f'<a href="{tag_url}">View</a>',
        ]
        data.append(row)

    return dt.create_response(data, records_total, records_filtered)


def tag_detail(request, tag_type, tag_number):
    model = TAG_MODELS[tag_type]
    schema = _tag_schema_or_404(tag_type)
    tag = get_object_or_404(model, tag_number=tag_number)

    datasets = []
    if tag.status == 'locked':
        field_map = {'p': 'physics_tag', 'e': 'evgen_tag', 's': 'simu_tag', 'r': 'reco_tag', 'k': 'background_tag'}
        datasets = Dataset.objects.filter(**{field_map[tag_type]: tag}).order_by('-created_at')

    defs = get_param_defs(tag_type)
    context = {
        'tag': tag,
        'tag_type': tag_type,
        'schema': schema,
        'datasets': datasets,
        'required_fields': [d['name'] for d in defs if d.get('required')],
        'optional_fields': [d['name'] for d in defs if not d.get('required')],
    }
    return render(request, 'pcs/tag_detail.html', context)


@_login_required_flash
def tag_create(request, tag_type):
    schema = _tag_schema_or_404(tag_type)

    if tag_type == 'p':
        FormClass = PhysicsTagForm
        form_kwargs = {}
    else:
        FormClass = SimpleTagForm
        form_kwargs = {'tag_type': tag_type}

    if request.method == 'POST':
        form = FormClass(request.POST, **form_kwargs)
        if form.is_valid():
            model = TAG_MODELS[tag_type]
            params = form.get_parameters()

            if tag_type == 'p':
                category = form.cleaned_data['category']
                tag_number = PhysicsTag.allocate_next(category)
                tag = PhysicsTag(
                    tag_number=tag_number,
                    category=category,
                    description=form.cleaned_data['description'],
                    parameters=params,
                    created_by=form.cleaned_data['created_by'],
                )
            else:
                tag_number = model.allocate_next()
                tag = model(
                    tag_number=tag_number,
                    description=form.cleaned_data['description'],
                    parameters=params,
                    created_by=form.cleaned_data['created_by'],
                )
            tag.save()
            messages.success(request, f"Tag {tag.tag_label} created.")
            log_epicprod_action(
                'web', 'tag_create', subject_key=tag.tag_label,
                username=getattr(request.user, 'username', ''))
            compose_url = reverse('pcs:tag_compose', kwargs={'tag_type': tag_type})
            return redirect(f'{compose_url}?selected={urlquote(tag.tag_label)}')
    else:
        form = FormClass(**form_kwargs)

    context = {
        'form': form,
        'tag_type': tag_type,
        'schema': schema,
    }
    template = 'pcs/tag_create_physics.html' if tag_type == 'p' else 'pcs/tag_create.html'
    return render(request, template, context)


def tag_compose(request, tag_type):
    """Split-panel browse + compose UI for physics tags."""
    schema = _tag_schema_or_404(tag_type)
    model = TAG_MODELS[tag_type]

    if tag_type == 'p':
        FormClass = PhysicsTagForm
        form_kwargs = {}
    else:
        FormClass = SimpleTagForm
        form_kwargs = {'tag_type': tag_type}

    selected_tag = None
    if request.method == 'POST':
        if not request.user.is_authenticated:
            from django.contrib.auth.views import redirect_to_login
            return redirect_to_login(request.get_full_path())
        form = FormClass(request.POST, **form_kwargs)
        if form.is_valid():
            params = form.get_parameters()
            if tag_type == 'p':
                category = form.cleaned_data['category']
                tag_number = PhysicsTag.allocate_next(category)
                tag = PhysicsTag(
                    tag_number=tag_number,
                    category=category,
                    description=form.cleaned_data['description'],
                    parameters=params,
                    created_by=form.cleaned_data['created_by'],
                )
            else:
                tag_number = model.allocate_next()
                tag = model(
                    tag_number=tag_number,
                    description=form.cleaned_data['description'],
                    parameters=params,
                    created_by=form.cleaned_data['created_by'],
                )
            tag.save()
            messages.success(request, f"Tag {tag.tag_label} created.")
            log_epicprod_action(
                'web', 'tag_create', subject_key=tag.tag_label,
                username=getattr(request.user, 'username', ''))
            compose_url = reverse('pcs:tag_compose', kwargs={'tag_type': tag_type})
            return redirect(f'{compose_url}?selected={urlquote(tag.tag_label)}')
    else:
        form = FormClass(**form_kwargs)
        selected_tag = request.GET.get('selected')

    qs = model.objects.order_by('-tag_number')
    if tag_type == 'p':
        qs = qs.select_related('category')
    tags_data = []
    for t in qs:
        entry = {
            'tag_number': t.tag_number,
            'tag_label': t.tag_label,
            'status': t.status,
            'description': t.description,
            'parameters': t.parameters,
            'created_by': t.created_by,
            'created_at': t.created_at.strftime('%Y-%m-%d %H:%M'),
            'updated_at': t.updated_at.strftime('%Y-%m-%d %H:%M'),
        }
        if tag_type == 'p':
            entry['category_digit'] = t.category.digit
            entry['category_name'] = t.category.name
        tags_data.append(entry)

    param_defs = get_param_defs(tag_type)
    choices_from_defs = {d['name']: d['choices'] for d in param_defs if d.get('choices')}
    filter_fields = [d['name'] for d in param_defs
                     if d['name'] not in ('notes', 'description')]

    # Peek at next tag suffix from PersistentState (read-only, no increment)
    from monitor_app.models import PersistentState
    state_keys = {'p': 'pcs_next_physics', 'e': 'pcs_next_evgen',
                  's': 'pcs_next_simu', 'r': 'pcs_next_reco',
                  'k': 'pcs_next_background'}
    try:
        ps = PersistentState.objects.get(id=1)
        next_suffix = ps.state_data.get(state_keys[tag_type], 1)
    except PersistentState.DoesNotExist:
        next_suffix = 1

    context = {
        'form': form,
        'tag_type': tag_type,
        'schema': schema,
        'tags_json': json.dumps(tags_data, default=str),
        'choices_json': json.dumps(choices_from_defs),
        'filter_fields_json': json.dumps(filter_fields),
        'param_defs_json': json.dumps(param_defs),
        'next_suffix': next_suffix,
        'username': request.user.username if request.user.is_authenticated else '',
        'selected_tag_json': json.dumps(selected_tag),
    }
    return render(request, 'pcs/tag_compose.html', context)


def tag_datasets(request, tag_type, tag_number):
    """On-demand 'used by' for a tag: the datasets composed with it, each with a
    representative task so the tag detail can link into the compose page (the
    task anchors the campaign). GET JSON, read-only."""
    if tag_type not in TAG_SCHEMAS:
        return JsonResponse({'error': 'Invalid tag type'}, status=400)
    model = get_tag_model(tag_type)
    tag = get_object_or_404(model, tag_number=tag_number)
    datasets = tag.datasets.select_related(
        'physics_tag', 'evgen_tag', 'simu_tag', 'reco_tag', 'background_tag',
    ).prefetch_related('prod_tasks').order_by('-created_at')
    out = []
    for ds in datasets:
        tasks = list(ds.prod_tasks.all())
        # A live (non-archive) task is the better link target; fall back to any.
        rep = [t for t in tasks if t.status != 'past_output'] or tasks
        out.append({
            'composed_name': ds.build_dataset_name(),
            'dataset_id': ds.id,
            'task_name': rep[0].name if rep else '',
            'task_count': len(tasks),
        })
    return JsonResponse({'datasets': out})


def param_defs_api(request, tag_type):
    if tag_type not in TAG_SCHEMAS:
        return JsonResponse({'error': 'Invalid tag type'}, status=400)
    if request.method == 'GET':
        return JsonResponse({'defs': get_param_defs(tag_type)})
    if not request.user.is_authenticated:
        return JsonResponse({'error': 'Login required'}, status=403)
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({'error': 'Invalid JSON'}, status=400)
        defs = data.get('defs')
        if not isinstance(defs, list):
            return JsonResponse({'error': 'defs must be a list'}, status=400)
        names_seen = set()
        for i, d in enumerate(defs):
            if not isinstance(d, dict) or not d.get('name'):
                return JsonResponse({'error': f'Invalid param def at index {i}'}, status=400)
            name = d['name'].strip()
            if name in names_seen:
                return JsonResponse({'error': f'Duplicate param name: {name}'}, status=400)
            names_seen.add(name)
            d['name'] = name
            d.setdefault('type', 'string')
            d.setdefault('required', False)
            d.setdefault('choices', [])
            d.setdefault('allow_other', True)
            d['sort_order'] = i
        save_param_defs(tag_type, defs)
        return JsonResponse({'ok': True, 'defs': defs})
    return JsonResponse({'error': 'Method not allowed'}, status=405)


@_login_required_flash
def tag_delete(request, tag_type, tag_number):
    if request.method != 'POST':
        return _post_only_redirect(
            request, reverse('pcs:tag_compose', kwargs={'tag_type': tag_type}),
            action_label='Tag delete')
    model = TAG_MODELS[tag_type]
    tag = get_object_or_404(model, tag_number=tag_number)
    if tag.status == 'locked':
        messages.error(request, f"Tag {tag.tag_label} is locked and cannot be deleted.")
        return redirect('pcs:tag_compose', tag_type=tag_type)
    if tag.created_by != request.user.username:
        messages.error(request, f"Only the creator ({tag.created_by}) can delete {tag.tag_label}.")
        return redirect('pcs:tag_compose', tag_type=tag_type)
    label = tag.tag_label
    tag.delete()
    messages.success(request, f"Tag {label} deleted.")
    log_epicprod_action(
        'web', 'tag_delete', subject_key=label,
        username=getattr(request.user, 'username', ''),
        sublevel='normal', live_default=True)
    return redirect('pcs:tag_compose', tag_type=tag_type)


@_login_required_flash
def tag_lock(request, tag_type, tag_number):
    compose_url = reverse('pcs:tag_compose', kwargs={'tag_type': tag_type})
    selected_url = f'{compose_url}?selected={tag_number}'
    if request.method != 'POST':
        return _post_only_redirect(request, selected_url, action_label='Tag lock')
    model = TAG_MODELS[tag_type]
    tag = get_object_or_404(model, tag_number=tag_number)
    if tag.created_by != request.user.username:
        messages.error(request, f"Only the creator ({tag.created_by}) can lock this tag.")
    elif tag.status == 'locked':
        messages.warning(request, f"Tag {tag.tag_label} is already locked.")
    else:
        tag.status = 'locked'
        tag.save(update_fields=['status', 'updated_at'])
        messages.success(request, f"Tag {tag.tag_label} locked. It can now be used in datasets.")
        log_epicprod_action(
            'web', 'tag_lock', subject_key=tag.tag_label,
            username=getattr(request.user, 'username', ''),
            sublevel='normal', live_default=True)
    return redirect(selected_url)


@_login_required_flash
def tag_edit(request, tag_type, tag_number):
    model = TAG_MODELS[tag_type]
    schema = _tag_schema_or_404(tag_type)
    tag = get_object_or_404(model, tag_number=tag_number)

    compose_url = reverse('pcs:tag_compose', kwargs={'tag_type': tag_type})
    selected_url = f'{compose_url}?selected={tag_number}'
    if tag.status == 'locked':
        messages.error(request, f"Tag {tag.tag_label} is locked and cannot be edited.")
        return redirect(selected_url)

    if tag_type == 'p':
        FormClass = PhysicsTagForm
        form_kwargs = {}
    else:
        FormClass = SimpleTagForm
        form_kwargs = {'tag_type': tag_type}

    if request.method == 'POST':
        form = FormClass(request.POST, **form_kwargs)
        if form.is_valid():
            tag.description = form.cleaned_data['description']
            tag.parameters = form.get_parameters()
            if tag_type == 'p':
                tag.category = form.cleaned_data['category']
            tag.save()
            messages.success(request, f"Tag {tag.tag_label} updated.")
            log_epicprod_action(
                'web', 'tag_edit', subject_key=tag.tag_label,
                username=getattr(request.user, 'username', ''))
            return redirect(selected_url)
    else:
        initial = {
            'description': tag.description,
            'created_by': tag.created_by,
        }
        if tag_type == 'p':
            initial['category'] = tag.category
        for k, v in tag.parameters.items():
            initial[f'param_{k}'] = v
        form = FormClass(initial=initial, **form_kwargs)

    context = {
        'form': form,
        'tag': tag,
        'tag_type': tag_type,
        'schema': schema,
        'editing': True,
    }
    template = 'pcs/tag_create_physics.html' if tag_type == 'p' else 'pcs/tag_create.html'
    return render(request, template, context)


# ── Datasets ──────────────────────────────────────────────────────

def _dataset_evgen_paths(ds):
    """An evgen-stage dataset's EVGEN paths: its matched Rucio DIDs, else
    the request's /EVGEN/ tail."""
    from .models import evgen_paths_for
    metadata = ds.metadata or {}
    if metadata.get('stage') != 'evgen':
        return []
    return evgen_paths_for((metadata.get('rucio') or {}).get('matched'),
                           (metadata.get('source') or {}).get('location'))


def _dataset_evgen_priority(ds, marks, tag_map=None):
    """The EVGEN paths a dataset resolves to with the PWG priority mark on
    each. An evgen-stage dataset resolves its own paths; any other stage
    (RECO, FULL editions) resolves through the evgen dataset carrying the
    same physics and evgen tags (``tag_map``: models.evgen_paths_by_tags).
    Empty when nothing resolves."""
    paths = _dataset_evgen_paths(ds)
    if not paths and tag_map:
        from .models import evgen_paths_for_tags
        paths = evgen_paths_for_tags(ds, tag_map)
    out = []
    for path in paths:
        mark = marks.get(path)
        out.append({
            'path': path,
            'priority': int(mark.priority) if mark else 0,
            'set_by': mark.priority_set_by if mark else '',
            'set_at': (timezone.localtime(mark.priority_set_at)
                       .strftime('%Y-%m-%d %H:%M')
                       if mark and mark.priority_set_at else ''),
        })
    return out


def datasets_compose(request):
    """Two-pane browse/create UI for datasets."""
    if request.method == 'POST' and request.user.is_authenticated:
        form = DatasetForm(request.POST)
        if form.is_valid():
            cd = form.cleaned_data
            ds = Dataset(
                scope=cd['scope'],
                detector_version=cd['detector_version'],
                detector_config=cd['detector_config'],
                physics_tag=cd['physics_tag'],
                evgen_tag=cd['evgen_tag'],
                simu_tag=cd['simu_tag'],
                reco_tag=cd['reco_tag'],
                background_tag=cd.get('background_tag'),
                description=cd.get('description', ''),
                metadata=cd.get('metadata') or None,
                created_by=cd['created_by'],
            )
            ds.save()
            messages.success(request, f"Dataset created: {ds.did}")
            log_epicprod_action(
                'web', 'dataset_create', subject_key=ds.composed_name or ds.dataset_name,
                username=getattr(request.user, 'username', ''),
                sublevel='normal', live_default=True)
            return redirect(f"{reverse('pcs:datasets_compose')}?selected={urlquote(ds.dataset_name)}")

    qs = Dataset.objects.filter(block_num=1).select_related(
        'physics_tag', 'evgen_tag', 'simu_tag', 'reco_tag', 'background_tag',
    ).order_by('-created_at')
    # PWG priority of an evgen dataset's EVGEN input(s), keyed by the
    # /EVGEN/... path as on the EVGEN inputs page (EPICPROD_EVGEN_INPUTS.md,
    # PWG marks); the detail panel shows and sets it.
    evgen_marks = {m.path: m for m in EvgenMark.objects.exclude(priority=0)}
    # Tags -> the EVGEN paths of the evgen dataset, so a RECO or FULL
    # edition resolves the same input as its evgen sibling.
    from .models import evgen_paths_by_tags
    tag_map = evgen_paths_by_tags()
    datasets_data = []
    for ds in qs:
        datasets_data.append({
            'id': ds.id,
            'evgen_priority': _dataset_evgen_priority(ds, evgen_marks, tag_map),
            'dataset_name': ds.dataset_name,
            'composed_name': ds.build_dataset_name(),
            'did': ds.did,
            'scope': ds.scope,
            'detector_version': ds.detector_version,
            'detector_config': ds.detector_config,
            'description': ds.description,
            'blocks': ds.blocks,
            'created_by': ds.created_by,
            'created_at': ds.created_at.strftime('%Y-%m-%d %H:%M'),
            'physics_tag': {'id': ds.physics_tag_id, 'label': ds.physics_tag.tag_label,
                            'description': ds.physics_tag.description, 'parameters': ds.physics_tag.parameters},
            'evgen_tag': {'id': ds.evgen_tag_id, 'label': ds.evgen_tag.tag_label,
                          'description': ds.evgen_tag.description, 'parameters': ds.evgen_tag.parameters},
            'simu_tag': {'id': ds.simu_tag_id, 'label': ds.simu_tag.tag_label,
                         'description': ds.simu_tag.description, 'parameters': ds.simu_tag.parameters},
            'reco_tag': {'id': ds.reco_tag_id, 'label': ds.reco_tag.tag_label,
                         'description': ds.reco_tag.description, 'parameters': ds.reco_tag.parameters},
            'background_tag': ({'id': ds.background_tag_id, 'label': ds.background_tag.tag_label,
                                'description': ds.background_tag.description, 'parameters': ds.background_tag.parameters}
                               if ds.background_tag_id else None),
        })

    # Full tag data for browsing and diffs
    tags_data = {}
    for ttype, model in TAG_MODELS_MAP.items():
        tag_list = []
        qs_tags = model.objects.order_by('tag_number')
        if ttype == 'p':
            qs_tags = qs_tags.select_related('category')
        for t in qs_tags:
            entry = {'id': t.id, 'tag_number': t.tag_number, 'label': t.tag_label,
                     'description': t.description, 'status': t.status,
                     'parameters': t.parameters, 'created_by': t.created_by,
                     'updated_at': t.updated_at.strftime('%Y-%m-%d %H:%M')}
            if ttype == 'p':
                entry['category_name'] = t.category.name
            tag_list.append(entry)
        tags_data[ttype] = tag_list

    context = {
        'datasets_json': json.dumps(datasets_data),
        'tags_json': json.dumps(tags_data),
        'selected_item_json': json.dumps(request.GET.get('selected') or None),
        'username': request.user.username if request.user.is_authenticated else '',
    }
    return render(request, 'pcs/dataset_compose.html', context)


def evgen_inputs(request):
    """EVGEN inputs: every epic:/EVGEN dataset in the recorded JLab Rucio
    inventory, with file counts, sizes, replicas, and the PCS evgen dataset
    each realizes. A pure read of the recorded inventory (the evgen-rucio
    snapshot plus the matched refs the EVGEN update writes) — no Rucio call
    in the render path. See docs/EPICPROD_EVGEN_INPUTS.md.
    """
    import json as _json
    import os as _os
    from .services import (RUCIO_SNAPSHOT_DIR, EVGEN_RUCIO_SNAPSHOT_NAME,
                           _rucio_evgen_entry)

    snap_path = _os.path.join(RUCIO_SNAPSHOT_DIR, EVGEN_RUCIO_SNAPSHOT_NAME)
    error = ''
    fetched_at = None
    records = []
    try:
        with open(snap_path) as f:
            snap = _json.load(f)
        fetched_at = snap.get('fetched_at')
        records = snap.get('datasets') or []
    except FileNotFoundError:
        error = ('No EVGEN inventory recorded yet — run Update EVGEN from '
                 'Rucio to fetch it.')
    except (OSError, ValueError) as e:
        error = f'EVGEN inventory unreadable: {e}'

    # Matched DID -> PCS evgen Dataset (written by refresh_evgen_rucio).
    matched = {}
    for ds in Dataset.objects.filter(metadata__stage='evgen',
                                     metadata__has_key='rucio'):
        for entry in (ds.metadata.get('rucio') or {}).get('matched') or []:
            did = entry.get('did')
            if did and did not in matched:
                matched[did] = ds

    from datetime import datetime as _dt

    def _replica_stamp(value):
        # Snapshot replicas carry Rucio's 'Thu, 04 Jun 2026 19:10:37 UTC'.
        try:
            return _dt.strptime(value, '%a, %d %b %Y %H:%M:%S %Z')
        except (TypeError, ValueError):
            return None

    rows = []
    for record in records:
        entry = _rucio_evgen_entry(record)
        ds = matched.get(entry['did'])
        stamps = [_replica_stamp(r.get('updated_at'))
                  for r in record.get('rse_replicas') or []]
        stamps = [s for s in stamps if s]
        rows.append({
            'did': entry['did'],
            'files': entry['file_count'],
            'bytes': entry['bytes'],
            'updated': max(stamps) if stamps else None,
            'rses': ', '.join(r['rse'] for r in entry['rses']),
            'complete': entry['complete'],
            'dataset': ds,
        })
    # Newest Rucio update first — the standing question this page answers;
    # every column stays click-sortable.
    rows.sort(key=lambda r: r['updated'] or _dt.min, reverse=True)

    totals = {
        'datasets': len(rows),
        'files': sum(r['files'] for r in rows),
        'bytes': sum(r['bytes'] for r in rows),
        'matched': sum(1 for r in rows if r['dataset'] is not None),
    }

    # PWG triage marks, keyed by the /EVGEN/... path — the vocabulary
    # shared by the inventory DIDs and the coverage worklist paths.
    from .models import EvgenMark
    marks = {m.path: m for m in EvgenMark.objects.all()}

    def _evgen_path(did):
        did = str(did or '')
        tail = did.partition(':')[2] if ':' in did else did
        return '/' + tail.lstrip('/')

    for row in rows:
        path = _evgen_path(row['did'])
        segs = [s for s in path.split('/') if s]
        row['path'] = path
        row['cls'] = segs[1] if len(segs) > 1 else ''
        mark = marks.get(path)
        row['obsolete'] = bool(mark and mark.obsolete)
        row['mark'] = mark if row['obsolete'] else None
        row['priority'] = int(mark.priority) if mark else 0
        row['pmark'] = mark if row['priority'] else None

    # Registration coverage of produced data: the convention-side EVGEN
    # path of every recorded RECO/FULL output (the payload's physics-path
    # law), diffed against this same recorded inventory — a registration
    # worklist. The task-overrides scan is a long build, so it serves as
    # a cached product (docs/CACHED_PRODUCTS.md); no Rucio call either
    # way.
    from monitor_app.cached_product import get_product
    from .services import XROOTD_EPIC_BASE

    def _build_coverage():
        registered_paths = set()
        for record in records:
            did = str(_rucio_evgen_entry(record)['did'] or '')
            registered_paths.add('/' + did.partition(':')[2].lstrip('/')
                                 if ':' in did else '/' + did.lstrip('/'))
        convention = {}
        for t in ProdTask.objects.all():
            for entry in (t.overrides or {}).get('outputs') or []:
                did = str(entry.get('did') or '')
                path = '/' + did.partition(':')[2].lstrip('/') \
                    if ':' in did else '/' + did.lstrip('/')
                segs = [s for s in path.split('/') if s]
                if len(segs) > 3 and segs[0] in ('RECO', 'FULL'):
                    epath = '/EVGEN/' + '/'.join(segs[3:])
                    info = convention.setdefault(
                        epath, {'evgen_path': epath,
                                'xrootd_path': XROOTD_EPIC_BASE + epath,
                                'reco_examples': set(), 'tasks': set()})
                    info['reco_examples'].add(path)
                    info['tasks'].add(t.composed_name)
        missing = []
        for epath in sorted(convention):
            if epath in registered_paths:
                continue
            info = convention[epath]
            missing.append({
                'evgen_path': epath,
                'xrootd_path': info['xrootd_path'],
                'reco_example': sorted(info['reco_examples'])[0],
                'tasks': sorted(info['tasks'])[:3],
            })
        return {'missing': missing, 'total': len(convention)}

    coverage = {'missing': [], 'total': 0}
    try:
        product = get_product(f'evgen_coverage:v1:{fetched_at or "none"}',
                              _build_coverage, ttl_seconds=3600)
        coverage = product.get('value') or coverage
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).error(
            'EVGEN coverage build failed: %s', exc)

    view = 'coverage' if request.GET.get('view') == 'coverage' \
        else 'inventory'

    # Marks attach after the cached-product read: the worklist build is
    # cacheable, the triage state is live.
    for entry in coverage['missing']:
        path = entry['evgen_path']
        segs = [s for s in path.split('/') if s]
        entry['cls'] = segs[1] if len(segs) > 1 else ''
        mark = marks.get(path)
        entry['obsolete'] = bool(mark and mark.obsolete)
        entry['mark'] = mark if entry['obsolete'] else None
        entry['priority'] = int(mark.priority) if mark else 0
        entry['pmark'] = mark if entry['priority'] else None
    # The worklist reads priority-first: the PWG's ordering is the
    # operations team's ordering; within a level, by path.
    coverage['missing'].sort(
        key=lambda e: (e['priority'] or 4, e['evgen_path']))
    # Obsolete-marked paths leave the registration worklist and its
    # count — the point of the triage; the Validity filter still
    # reaches them for review.
    coverage_missing_total = sum(
        1 for e in coverage['missing'] if not e['obsolete'])

    # Natural filters over the active view: physics class, PCS match and
    # completeness (inventory only), and validity — everything shows by
    # default, obsolete entries muted with attribution.
    from collections import Counter
    from urllib.parse import urlencode

    selected = {
        'cls': (request.GET.get('cls') or '').strip(),
        'matched': (request.GET.get('matched') or '').strip(),
        'complete': (request.GET.get('complete') or '').strip(),
        'validity': (request.GET.get('validity') or '').strip(),
        'priority': (request.GET.get('priority') or '').strip(),
    }
    if selected['validity'] not in ('', 'current', 'obsolete'):
        selected['validity'] = ''
    if selected['priority'] not in ('', '1', '2', '3', 'unset'):
        selected['priority'] = ''

    def _qs(**over):
        params = {}
        if view == 'coverage':
            params['view'] = 'coverage'
        for key in ('cls', 'matched', 'complete', 'validity', 'priority'):
            value = over.get(key, selected[key])
            if value:
                params[key] = value
        return '?' + urlencode(params) if params else request.path

    population = rows if view != 'coverage' else coverage['missing']
    filters = []
    cls_counts = Counter(x['cls'] for x in population if x.get('cls'))
    filters.append({
        'key': 'cls', 'label': 'Physics class', 'selected': selected['cls'],
        'all_url': _qs(cls=''),
        'options': [{'value': v, 'count': cls_counts[v], 'url': _qs(cls=v)}
                    for v in sorted(cls_counts)]})
    if view != 'coverage':
        matched_counts = Counter(
            'matched' if r['dataset'] else 'unmatched' for r in rows)
        filters.append({
            'key': 'matched', 'label': 'PCS match',
            'selected': selected['matched'], 'all_url': _qs(matched=''),
            'options': [{'value': v, 'count': matched_counts[v],
                         'url': _qs(matched=v)}
                        for v in sorted(matched_counts)]})
        complete_counts = Counter(
            'complete' if r['complete'] else 'partial' for r in rows)
        filters.append({
            'key': 'complete', 'label': 'Complete datasets',
            'selected': selected['complete'], 'all_url': _qs(complete=''),
            'options': [{'value': v, 'count': complete_counts[v],
                         'url': _qs(complete=v)}
                        for v in sorted(complete_counts)]})
    validity_counts = Counter(
        'obsolete' if x['obsolete'] else 'current' for x in population)
    filters.append({
        'key': 'validity', 'label': 'Validity',
        'selected': selected['validity'], 'all_url': _qs(validity=''),
        'options': [{'value': v, 'count': validity_counts[v],
                     'url': _qs(validity=v)}
                    for v in ('current', 'obsolete')
                    if validity_counts.get(v)]})
    priority_counts = Counter(
        str(x['priority']) if x['priority'] else 'unset' for x in population)
    filters.append({
        'key': 'priority', 'label': 'Priority',
        'selected': selected['priority'], 'all_url': _qs(priority=''),
        'options': [{'value': v, 'count': priority_counts[v],
                     'url': _qs(priority=v)}
                    for v in ('1', '2', '3', 'unset')
                    if priority_counts.get(v)]})

    def _keep(x, is_row):
        if selected['priority'] == 'unset' and x['priority']:
            return False
        if selected['priority'] in ('1', '2', '3') \
                and str(x['priority']) != selected['priority']:
            return False
        if selected['cls'] and x['cls'] != selected['cls']:
            return False
        if is_row:
            if selected['matched'] == 'matched' and not x['dataset']:
                return False
            if selected['matched'] == 'unmatched' and x['dataset']:
                return False
            if selected['complete'] == 'complete' and not x['complete']:
                return False
            if selected['complete'] == 'partial' and x['complete']:
                return False
        if effective_validity == 'current' and x['obsolete']:
            return False
        if effective_validity == 'obsolete' and not x['obsolete']:
            return False
        return True

    effective_validity = selected['validity']
    if view == 'coverage' and not effective_validity:
        effective_validity = 'current'
    if view != 'coverage':
        rows = [r for r in rows if _keep(r, True)]
    else:
        coverage['missing'] = [e for e in coverage['missing']
                               if _keep(e, False)]

    active_filters = [
        {'label': f['label'], 'value': f['selected']}
        for f in filters if f['selected']]

    return render(request, 'pcs/evgen_inputs.html', {
        'rows': rows, 'totals': totals, 'fetched_at': fetched_at,
        'error': error, 'view': view,
        'coverage_missing': coverage['missing'],
        'coverage_missing_total': coverage_missing_total,
        'coverage_total': coverage['total'],
        'filters': filters,
        'active_filters': active_filters,
        'clear_all_url': ('?view=coverage' if view == 'coverage'
                          else request.path),
        'any_filter': bool(active_filters),
    })


def evgen_inputs_update(request):
    """No-JS POST fallback for the EVGEN inputs page's update button — the
    pcs_catalog_evgen_update twin, returning here. POST-only."""
    if request.method != 'POST':
        return _post_only_redirect(
            request, reverse('pcs:evgen_inputs'),
            action_label='Update EVGEN from Rucio')
    from .services import evgen_rucio_update_request, ServiceError
    user = getattr(request.user, 'username', '') or 'evgen_rucio'
    try:
        evgen_rucio_update_request(created_by=user)
    except ServiceError as e:
        messages.error(request, e.detail)
        return redirect(reverse('pcs:evgen_inputs'))
    messages.success(request,
                     'EVGEN update queued — refreshing in the background.')
    return redirect(reverse('pcs:evgen_inputs'))


def _build_find_corpus():
    """Search corpus for the find-data page: every produced Rucio DID on
    the task records, every registered EVGEN dataset in the recorded
    inventory, and every convention-implied EVGEN path the inventory
    lacks. Entries carry scope/name for render-time URL building (never
    a baked path) and a lowercase match blob. Local state only — no
    Rucio call.
    """
    import json as _json
    import os as _os
    from .services import (RUCIO_SNAPSHOT_DIR, EVGEN_RUCIO_SNAPSHOT_NAME,
                           _rucio_evgen_entry, XROOTD_EPIC_BASE,
                           _extract_past_filters, _extract_evgen_did_filters)

    def _did_parts(did):
        scope, _, name = str(did).partition(':')
        return scope, name.lstrip('/')

    generator_re = re.compile(r'^[A-Za-z].*\d.*[.-]\d')

    def _augment_facets(facets, segs):
        # Fields the shared axes leave empty on these name shapes: the
        # species segment after BEAMGAS, and the generator-version
        # segment (pythia8.306-1.0, GETaLM1.0.0-1.1).
        if not facets.get('species') and 'BEAMGAS' in segs:
            i = segs.index('BEAMGAS')
            if len(segs) > i + 1:
                facets['species'] = segs[i + 1]
        facets['generator'] = next(
            (s for s in segs if generator_re.match(s)), '')
        return facets

    entries = []
    seen_dids = set()
    produced_paths = {}
    # PWG priority marks keyed by /EVGEN/ path (EPICPROD_EVGEN_INPUTS.md);
    # produced entries carry their task's resolved priority, EVGEN
    # entries the mark on their own path.
    pwg_marks = {m.path: int(m.priority)
                 for m in EvgenMark.objects.filter(priority__gt=0)}
    # Only the columns the corpus and the priority preload read: a task's
    # inputs and source location come from its dataset's metadata, the
    # tag fallback from the dataset's physics/evgen tags and sample.
    for t in annotate_pwg_priority(
            ProdTask.objects.select_related('campaign', 'dataset')
            .only('overrides', 'name', 'description', 'csv_file',
                  'campaign__name', 'dataset__composed_name',
                  'dataset__metadata', 'dataset__physics_tag',
                  'dataset__evgen_tag', 'dataset__sample_name')):
        camp = t.campaign.name if t.campaign else ''
        for out in (t.overrides or {}).get('outputs') or []:
            did = str(out.get('did') or '')
            if not did or ':' not in did:
                continue
            scope, name = _did_parts(did)
            path = '/' + name
            produced_paths.setdefault(path, did)
            kind = str(out.get('stage') or 'output')
            blob = ' '.join([did, camp, t.composed_name or '',
                             t.description or '', kind]).lower()
            if did in seen_dids:
                continue
            seen_dids.add(did)
            segs = [s for s in name.split('/') if s]
            facets = _augment_facets(_extract_past_filters(did), segs)
            facets['version'] = (str(out.get('version') or '')
                                 or (segs[1] if len(segs) > 1 else ''))
            entries.append({
                'kind': kind,
                'did': did, 'scope': scope, 'name': name,
                'campaign': camp,
                'files': out.get('file_count'), 'bytes': out.get('bytes'),
                'facets': facets,
                'pwg_priority': t.pwg_priority,
                'pwg_resolved': bool(t.evgen_paths),
                'blob': blob + (f' pwg priority {t.pwg_priority}'
                                if t.pwg_priority else ''),
            })

    registered_paths = set()
    try:
        with open(_os.path.join(RUCIO_SNAPSHOT_DIR,
                                EVGEN_RUCIO_SNAPSHOT_NAME)) as f:
            snap = _json.load(f)
    except (OSError, ValueError) as e:
        logging.getLogger(__name__).error(
            'find corpus: EVGEN snapshot unreadable: %s', e)
        snap = {}
    for record in snap.get('datasets') or []:
        entry = _rucio_evgen_entry(record)
        did = str(entry['did'] or '')
        if not did or ':' not in did:
            continue
        scope, name = _did_parts(did)
        registered_paths.add('/' + name)
        facets = _augment_facets(_extract_evgen_did_filters(did),
                                 [s for s in name.split('/') if s])
        facets['version'] = ''
        pwg = pwg_marks.get('/' + name, 0)
        entries.append({
            'kind': 'EVGEN',
            'did': did, 'scope': scope, 'name': name,
            'campaign': '',
            'files': entry['file_count'], 'bytes': entry['bytes'],
            'facets': facets,
            'pwg_priority': pwg, 'pwg_resolved': True,
            'blob': (did + ' evgen registered'
                     + (f' pwg priority {pwg}' if pwg else '')).lower(),
        })

    # Convention-implied EVGEN paths absent from the registered inventory
    # (the registration-coverage population): the answer for these is the
    # direct xrootd path, plus a produced dataset whose page states it.
    implied = {}
    for path, did in produced_paths.items():
        segs = [s for s in path.split('/') if s]
        if len(segs) > 3 and segs[0] in ('RECO', 'FULL'):
            epath = '/EVGEN/' + '/'.join(segs[3:])
            implied.setdefault(epath, did)
    for epath in sorted(set(implied) - registered_paths):
        reco_did = implied[epath]
        scope, name = _did_parts(reco_did)
        facets = _augment_facets(
            _extract_evgen_did_filters('epic:' + epath),
            [s for s in epath.split('/') if s])
        facets['version'] = ''
        pwg = pwg_marks.get(epath, 0)
        entries.append({
            'kind': 'EVGEN (unregistered)',
            'did': '', 'scope': scope, 'name': name,
            'campaign': '',
            'evgen_path': epath,
            'xrootd_path': XROOTD_EPIC_BASE + epath,
            'files': None, 'bytes': None,
            'facets': facets,
            'pwg_priority': pwg, 'pwg_resolved': True,
            'blob': (epath + ' ' + reco_did + ' evgen unregistered'
                     + (f' pwg priority {pwg}' if pwg else '')).lower(),
        })
    return entries


def _find_corpus():
    """The cached find corpus (empty list on build failure, error logged)."""
    from monitor_app.cached_product import get_product
    # A PWG mark change re-keys the corpus, so the badges follow at once.
    marks_stamp = EvgenMark.objects.aggregate(m=Max('priority_set_at'))['m']
    key = f"pcs_find_corpus:v6:{marks_stamp.isoformat() if marks_stamp else 'none'}"
    product = get_product(key, _build_find_corpus, ttl_seconds=900)
    return product.get('value') or []


def _find_hits(corpus, q):
    """Match a find query against the corpus: words are ANDed as
    substrings; a word prefixed with '-' excludes entries containing it."""
    tokens = q.lower().split()
    include = [t for t in tokens if not t.startswith('-')]
    exclude = [t[1:] for t in tokens if t.startswith('-') and len(t) > 1]
    return [e for e in corpus
            if all(t in e['blob'] for t in include)
            and not any(t in e['blob'] for t in exclude)]


def find_data(request):
    """Find data: one search field over everything recorded — produced
    Rucio DIDs across all campaigns, the registered EVGEN inventory, and
    unregistered convention-implied EVGEN paths. Words are ANDed as
    substrings and '-word' excludes; a single hit redirects straight to
    its dataset page. The corpus is a cached product; the render path
    makes no Rucio call. The Brains dialog runs on the DISpatcher
    engine over the same bar.
    """
    q = (request.GET.get('q') or '').strip()
    engine = (request.GET.get('engine') or '').strip()
    hits = None
    if q:
        corpus = []
        try:
            corpus = _find_corpus()
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).error(
                'find corpus build failed: %s', exc)
            messages.error(request, f'Search index unavailable: {exc}')
        hits = _find_hits(corpus, q)
        kind_rank = {'EVGEN (unregistered)': 2}
        hits.sort(key=lambda e: (kind_rank.get(e['kind'], 1),
                                 e.get('did') or e.get('evgen_path') or ''))
        if len(hits) == 1 and hits[0]['did'] and engine != 'brains':
            return redirect(reverse('pcs:rucio_did_detail',
                                    args=[hits[0]['scope'], hits[0]['name']]))
    from monitor_app.panda.constants import AI_MODEL
    family = next((f for f in ('sonnet', 'opus', 'haiku', 'fable')
                   if f in AI_MODEL.lower()), '')
    brains_id = (request.GET.get('brains') or '').strip()
    if brains_id and not _BRAINS_ID_RE.match(brains_id):
        brains_id = ''
    return render(request, 'pcs/find.html', {
        'q': q, 'hits': hits, 'engine': engine,
        'has_unregistered': bool(hits) and any(
            e['kind'] == 'EVGEN (unregistered)' for e in hits),
        'brains_model': family.capitalize() or AI_MODEL,
        'brains_id': brains_id,
    })


_BRAINS_ID_RE = re.compile(r'^brains-[a-f0-9]{12}$')


def find_brains_post(request):
    """Queue one turn of a Brains dialog from the find page. POST JSON
    {conversation_id?, message}; returns {conversation_id}. The answer
    arrives via the brains_answer SSE event and the conversation GET."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)
    try:
        body = json.loads(request.body or b'{}')
    except ValueError as e:
        return JsonResponse({'error': f'unparseable body: {e}'}, status=400)
    message = str(body.get('message') or '').strip()
    if not message:
        return JsonResponse({'error': 'empty message'}, status=400)
    conversation_id = str(body.get('conversation_id') or '').strip()
    if conversation_id and not _BRAINS_ID_RE.match(conversation_id):
        return JsonResponse({'error': 'bad conversation id'}, status=400)
    if not conversation_id:
        import uuid
        conversation_id = f'brains-{uuid.uuid4().hex[:12]}'
    # Crawler-bill insurance: modest per-session ceiling on engine turns.
    rl_key = 'brains-rl:' + (request.session.session_key
                             or request.META.get('REMOTE_ADDR', 'unknown'))
    turns = cache.get(rl_key, 0)
    if turns >= 30:
        return JsonResponse(
            {'error': 'Brains turn limit reached — try again later.'},
            status=429)
    cache.set(rl_key, turns + 1, 600)
    username = (getattr(request.user, 'username', '') or 'web user')
    # Ground the engine in what the page currently shows: the applied
    # search and its kind breakdown ride into the turn's context.
    page_state = ''
    page_q = str(body.get('page_q') or '').strip()
    if page_q:
        try:
            hits = _find_hits(_find_corpus(), page_q)
            kinds = {}
            for e in hits:
                kinds[e['kind']] = kinds.get(e['kind'], 0) + 1
            breakdown = ', '.join(f'{k} {n}' for k, n in sorted(kinds.items()))
            page_state = (f"applied search '{page_q}': {len(hits)} matches"
                          + (f' ({breakdown})' if breakdown else ''))
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).error(
                'brains page-state build failed: %s', exc)
    from .services import brains_query_request, ServiceError
    try:
        brains_query_request(conversation_id=conversation_id,
                             username=username, message=message,
                             page_state=page_state)
    except ServiceError as e:
        return JsonResponse({'error': e.detail}, status=e.status)
    return JsonResponse({'conversation_id': conversation_id})


def find_brains_event(request):
    """Record an applied search into a Brains dialog's durable narrative.
    POST JSON {conversation_id, q}; record-only, no LLM run."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)
    try:
        body = json.loads(request.body or b'{}')
    except ValueError as e:
        return JsonResponse({'error': f'unparseable body: {e}'}, status=400)
    conversation_id = str(body.get('conversation_id') or '').strip()
    query = str(body.get('q') or '').strip()
    if not _BRAINS_ID_RE.match(conversation_id):
        return JsonResponse({'error': 'bad conversation id'}, status=400)
    if not query:
        return JsonResponse({'error': 'empty query'}, status=400)
    username = (getattr(request.user, 'username', '') or 'web user')
    from .services import brains_event_request, ServiceError
    try:
        brains_event_request(conversation_id=conversation_id,
                             username=username, query=query)
    except ServiceError as e:
        return JsonResponse({'error': e.detail}, status=e.status)
    return JsonResponse({'ok': True})


def find_brains_conversation(request, conversation_id):
    """Serve a Brains conversation record (bot-written, web-read)."""
    if not _BRAINS_ID_RE.match(conversation_id):
        return JsonResponse({'error': 'bad conversation id'}, status=400)
    from django.conf import settings as dj_settings
    import os as _os
    path = _os.path.join(
        getattr(dj_settings, 'SWF_TMP_DIR', '/data/swf-tmp'), 'brains',
        f'{conversation_id}.json')
    try:
        with open(path) as f:
            return JsonResponse(json.load(f))
    except FileNotFoundError:
        return JsonResponse({'conversation_id': conversation_id, 'turns': []})
    except (OSError, ValueError) as e:
        logging.getLogger(__name__).error(
            'brains conversation read failed (%s): %s', path, e)
        return JsonResponse({'error': 'conversation unreadable'}, status=500)


def datasets_list(request):
    columns = [
        {'name': 'dataset_name', 'title': 'Dataset Name', 'orderable': True},
        {'name': 'physics_tag__tag_label', 'title': 'Physics', 'orderable': True},
        {'name': 'evgen_tag__tag_label', 'title': 'EvGen', 'orderable': True},
        {'name': 'simu_tag__tag_label', 'title': 'Simu', 'orderable': True},
        {'name': 'reco_tag__tag_label', 'title': 'Reco', 'orderable': True},
        {'name': 'background_tag__tag_label', 'title': 'Background', 'orderable': True},
        {'name': 'blocks', 'title': 'Blocks', 'orderable': True},
        {'name': 'created_at', 'title': 'Created', 'orderable': True},
    ]
    context = {
        'table_title': 'Datasets',
        'table_description': 'All datasets registered in PCS.',
        'ajax_url': reverse('pcs:datasets_datatable_ajax'),
        'columns': columns,
    }
    return render(request, 'pcs/datasets_list.html', context)


def datasets_datatable_ajax(request):
    col_names = [
        'dataset_name', 'physics_tag__tag_label', 'evgen_tag__tag_label',
        'simu_tag__tag_label', 'reco_tag__tag_label', 'background_tag__tag_label',
        'blocks', 'created_at',
    ]
    dt = DataTablesProcessor(request, col_names, default_order_column=7, default_order_direction='desc')

    # Only show block 1 rows (one row per logical dataset)
    qs = Dataset.objects.filter(block_num=1).select_related(
        'physics_tag', 'evgen_tag', 'simu_tag', 'reco_tag', 'background_tag'
    )

    records_total = Dataset.objects.filter(block_num=1).count()
    search_fields = ['dataset_name', 'physics_tag__tag_label', 'evgen_tag__tag_label',
                     'simu_tag__tag_label', 'reco_tag__tag_label', 'background_tag__tag_label']
    qs = dt.apply_search(qs, search_fields)
    records_filtered = qs.count()
    qs = qs.order_by(dt.get_order_by())
    page = dt.apply_pagination(qs)

    data = []
    for ds in page:
        detail_url = (f"{reverse('pcs:datasets_compose')}"
                      f"?selected={urlquote(ds.dataset_name)}")
        p_url = f"{reverse('pcs:tag_compose', args=['p'])}?selected={ds.physics_tag.tag_number}"
        e_url = f"{reverse('pcs:tag_compose', args=['e'])}?selected={ds.evgen_tag.tag_number}"
        s_url = f"{reverse('pcs:tag_compose', args=['s'])}?selected={ds.simu_tag.tag_number}"
        r_url = f"{reverse('pcs:tag_compose', args=['r'])}?selected={ds.reco_tag.tag_number}"
        # Name: the tag-composed name (build_dataset_name); the internal
        # csv_import.<hash> dataset_name is plumbing and is never shown.
        if ds.background_tag_id:
            k_url = f"{reverse('pcs:tag_compose', args=['k'])}?selected={ds.background_tag.tag_number}"
            k_cell = f'<a href="{k_url}" title="{ds.background_tag.description}">{ds.background_tag.tag_label}</a>'
        else:
            k_cell = '-'
        data.append([
            f'<a href="{detail_url}">{ds.composed_name}</a>',
            f'<a href="{p_url}" title="{ds.physics_tag.description}">{ds.physics_tag.tag_label}</a>',
            f'<a href="{e_url}" title="{ds.evgen_tag.description}">{ds.evgen_tag.tag_label}</a>',
            f'<a href="{s_url}" title="{ds.simu_tag.description}">{ds.simu_tag.tag_label}</a>',
            f'<a href="{r_url}" title="{ds.reco_tag.description}">{ds.reco_tag.tag_label}</a>',
            k_cell,
            str(ds.blocks),
            format_datetime(ds.created_at),
        ])

    return dt.create_response(data, records_total, records_filtered)


def dataset_detail(request, pk):
    dataset = get_object_or_404(
        Dataset.objects.select_related('physics_tag', 'evgen_tag', 'simu_tag', 'reco_tag'),
        pk=pk,
    )
    blocks = Dataset.objects.filter(dataset_name=dataset.dataset_name).order_by('block_num')

    # Reverse references — tasks that use this dataset and in what role.
    # Output: legacy FK or override list contains DID. Input: legacy single
    # override or list contains DID. Intermediate: list only.
    did = dataset.did
    output_tasks = (ProdTask.objects
                    .filter(Q(dataset=dataset)
                            | Q(overrides__output_dataset_dids__contains=[did]))
                    .distinct().order_by('name'))
    input_tasks = (ProdTask.objects
                   .filter(Q(overrides__input_dataset_did=did)
                           | Q(overrides__input_dataset_dids__contains=[did]))
                   .distinct().order_by('name'))
    intermediate_tasks = (ProdTask.objects
                          .filter(overrides__intermediate_dataset_dids__contains=[did])
                          .order_by('name'))

    context = {
        'dataset': dataset,
        'blocks': blocks,
        'output_tasks': output_tasks,
        'input_tasks': input_tasks,
        'intermediate_tasks': intermediate_tasks,
    }
    return render(request, 'pcs/dataset_detail.html', context)


@_login_required_flash
def dataset_create(request):
    if request.method == 'POST':
        form = DatasetForm(request.POST)
        if form.is_valid():
            cd = form.cleaned_data
            ds = Dataset(
                scope=cd['scope'],
                detector_version=cd['detector_version'],
                detector_config=cd['detector_config'],
                physics_tag=cd['physics_tag'],
                evgen_tag=cd['evgen_tag'],
                simu_tag=cd['simu_tag'],
                reco_tag=cd['reco_tag'],
                background_tag=cd.get('background_tag'),
                description=cd.get('description', ''),
                metadata=cd.get('metadata') or None,
                created_by=cd['created_by'],
            )
            ds.save()
            messages.success(request, f"Dataset created: {ds.did}")
            log_epicprod_action(
                'web', 'dataset_create', subject_key=ds.composed_name or ds.dataset_name,
                username=getattr(request.user, 'username', ''),
                sublevel='normal', live_default=True)
            return redirect('pcs:dataset_detail', pk=ds.pk)
    else:
        form = DatasetForm()
    return render(request, 'pcs/dataset_create.html', {'form': form})


@_login_required_flash
def dataset_add_block(request, pk):
    if request.method != 'POST':
        return _post_only_redirect(
            request, reverse('pcs:dataset_detail', kwargs={'pk': pk}),
            action_label='Add-block')
    dataset = get_object_or_404(Dataset, pk=pk)
    new_block_num = dataset.blocks + 1
    Dataset.objects.filter(dataset_name=dataset.dataset_name).update(blocks=new_block_num)
    new_block = Dataset.objects.create(
        dataset_name=dataset.dataset_name,
        scope=dataset.scope,
        detector_version=dataset.detector_version,
        detector_config=dataset.detector_config,
        physics_tag=dataset.physics_tag,
        evgen_tag=dataset.evgen_tag,
        simu_tag=dataset.simu_tag,
        reco_tag=dataset.reco_tag,
        background_tag=dataset.background_tag,
        block_num=new_block_num,
        blocks=new_block_num,
        did=f"{dataset.scope}:{dataset.dataset_name}.b{new_block_num}",
        description=dataset.description,
        metadata=dataset.metadata,
        created_by=request.user.username if request.user.is_authenticated else 'unknown',
    )
    messages.success(request, f"Block {new_block_num} added: {new_block.did}")
    log_epicprod_action(
        'web', 'dataset_block_add', subject_key=new_block.did,
        username=getattr(request.user, 'username', ''))
    return redirect('pcs:dataset_detail', pk=dataset.pk)


# ── Production Configs ────────────────────────────────────────────

def prod_configs_compose(request):
    """Two-pane browse/create/edit UI for production configs."""
    if request.method == 'POST' and request.user.is_authenticated:
        editing_pk = request.POST.get('editing_pk')
        if editing_pk:
            instance = get_object_or_404(ProdConfig, pk=editing_pk)
            form = ProdConfigForm(request.POST, instance=instance)
        else:
            form = ProdConfigForm(request.POST)
        if form.is_valid():
            pc = form.save()
            messages.success(request, f"Config '{pc.name}' {'updated' if editing_pk else 'created'}.")
            log_epicprod_action(
                'web', 'config_edit' if editing_pk else 'config_create',
                subject_key=pc.name,
                username=getattr(request.user, 'username', ''))
            return redirect(f"{reverse('pcs:prod_configs_compose')}?selected={urlquote(pc.name)}")

    qs = ProdConfig.objects.order_by('-updated_at')
    configs_data = []
    for pc in qs:
        configs_data.append({
            'id': pc.id,
            'name': pc.name,
            'description': pc.description,
            'bg_mixing': pc.bg_mixing,
            'bg_cross_section': pc.bg_cross_section,
            'bg_evtgen_file': pc.bg_evtgen_file,
            'copy_reco': pc.copy_reco,
            'copy_full': pc.copy_full,
            'copy_log': pc.copy_log,
            'use_rucio': pc.use_rucio,
            'jug_xl_tag': pc.jug_xl_tag,
            'container_image': pc.container_image,
            'target_hours_per_job': str(pc.target_hours_per_job) if pc.target_hours_per_job else '',
            'events_per_task': pc.events_per_task,
            'panda_site': pc.panda_site,
            'panda_queue': pc.panda_queue,
            'panda_working_group': pc.panda_working_group,
            'panda_resource_type': pc.panda_resource_type,
            'rucio_rse': pc.rucio_rse,
            'rucio_replication_rules': pc.rucio_replication_rules,
            'condor_template': pc.condor_template,
            'data': pc.data or {},
            'created_by': pc.created_by,
            'created_at': pc.created_at.strftime('%Y-%m-%d %H:%M'),
            'updated_at': pc.updated_at.strftime('%Y-%m-%d %H:%M'),
        })

    context = {
        'configs_json': json.dumps(configs_data),
        'selected_item_json': json.dumps(request.GET.get('selected') or None),
        'username': request.user.username if request.user.is_authenticated else '',
        'prod_config_scout_mode_json': json.dumps(
            _prod_config_scout_mode_pref(
                request.user.username if request.user.is_authenticated else ''
            )
        ),
    }
    return render(request, 'pcs/prod_config_compose.html', context)


def prod_configs_list(request):
    columns = [
        {'name': 'name', 'title': 'Name', 'orderable': True},
        {'name': 'description', 'title': 'Description', 'orderable': True},
        {'name': 'jug_xl_tag', 'title': 'JUG_XL', 'orderable': True},
        {'name': 'target_hours_per_job', 'title': 'Hours/Job', 'orderable': True},
        {'name': 'events_per_task', 'title': 'Events/Task', 'orderable': True},
        {'name': 'created_by', 'title': 'Created By', 'orderable': True},
        {'name': 'updated_at', 'title': 'Updated', 'orderable': True},
    ]
    context = {
        'table_title': 'Production Configs',
        'table_description': 'Reusable production configuration templates for job submission.',
        'ajax_url': reverse('pcs:prod_configs_datatable_ajax'),
        'columns': columns,
    }
    return render(request, 'pcs/prod_configs_list.html', context)


def prod_configs_datatable_ajax(request):
    col_names = ['name', 'description', 'jug_xl_tag', 'target_hours_per_job',
                 'events_per_task', 'created_by', 'updated_at']
    dt = DataTablesProcessor(request, col_names, default_order_column=6, default_order_direction='desc')

    qs = ProdConfig.objects.all()
    records_total = qs.count()
    search_fields = ['name', 'description', 'created_by', 'jug_xl_tag']
    qs = dt.apply_search(qs, search_fields)
    records_filtered = qs.count()
    qs = qs.order_by(dt.get_order_by())
    page = dt.apply_pagination(qs)

    data = []
    for pc in page:
        detail_url = reverse('pcs:prod_config_detail', args=[pc.pk])
        data.append([
            f'<a href="{detail_url}">{pc.name}</a>',
            pc.description[:80] + ('...' if len(pc.description) > 80 else ''),
            pc.jug_xl_tag or '-',
            str(pc.target_hours_per_job) if pc.target_hours_per_job else '-',
            str(pc.events_per_task) if pc.events_per_task else '-',
            pc.created_by,
            format_datetime(pc.updated_at),
        ])

    return dt.create_response(data, records_total, records_filtered)


def prod_config_detail(request, pk):
    config = get_object_or_404(ProdConfig, pk=pk)
    return render(request, 'pcs/prod_config_detail.html', {'config': config})


@_login_required_flash
def prod_config_create(request):
    if request.method == 'POST':
        form = ProdConfigForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, f"Production config '{form.instance.name}' created.")
            log_epicprod_action(
                'web', 'config_create', subject_key=form.instance.name,
                username=getattr(request.user, 'username', ''))
            return redirect('pcs:prod_config_detail', pk=form.instance.pk)
    else:
        form = ProdConfigForm()
    return render(request, 'pcs/prod_config_form.html', {
        'form': form,
        'username': request.user.username if request.user.is_authenticated else '',
        'prod_config_scout_mode_json': json.dumps(
            _prod_config_scout_mode_pref(request.user.username)
        ),
    })


@_login_required_flash
def prod_config_edit(request, pk):
    config = get_object_or_404(ProdConfig, pk=pk)
    if request.method == 'POST':
        form = ProdConfigForm(request.POST, instance=config)
        if form.is_valid():
            form.save()
            messages.success(request, f"Production config '{config.name}' updated.")
            log_epicprod_action(
                'web', 'config_edit', subject_key=config.name,
                username=getattr(request.user, 'username', ''))
            return redirect('pcs:prod_config_detail', pk=config.pk)
    else:
        form = ProdConfigForm(instance=config)
    return render(request, 'pcs/prod_config_form.html', {
        'form': form,
        'editing': True,
        'config': config,
        'username': request.user.username if request.user.is_authenticated else '',
        'prod_config_scout_mode_json': json.dumps(
            _prod_config_scout_mode_pref(request.user.username)
        ),
    })


# ── Production Tasks ─────────────────────────────────────────────

TAG_MODELS_MAP = {'p': PhysicsTag, 'e': EvgenTag, 's': SimuTag, 'r': RecoTag, 'k': BackgroundTag}


LIFECYCLE_KEYS = ('past', 'last', 'current', 'future')


@_login_required_flash
def pcs_catalog_csv_update(request):
    """POST handler for the 'Update from CSV' button on the catalog.

    Runs the default-datasets CSV import service and redirects back to
    the catalog with a flash summary. POST-only.
    """
    if request.method != 'POST':
        return _post_only_redirect(
            request, reverse('pcs:pcs_catalog'),
            action_label='Update from CSV')
    from .services import import_default_datasets_csv, ServiceError
    try:
        summary = import_default_datasets_csv(
            created_by=getattr(request.user, 'username', '') or 'csv_import',
        )
    except (ServiceError, FileNotFoundError, OSError) as e:
        messages.error(request, f'CSV import failed: {e}')
        return redirect(reverse('pcs:pcs_catalog'))
    msg = (f'CSV import: {summary["created"]} new, '
           f'{summary["updated"]} updated, '
           f'{summary["requests_created"]} new / '
           f'{summary["requests_updated"]} updated requests, '
           f'{len(summary["errors"])} errors '
           f'(of {summary["rows"]} rows)')
    if summary['errors']:
        messages.warning(request, msg)
    else:
        messages.success(request, msg)
    return redirect(reverse('pcs:pcs_catalog'))


@_login_required_flash
def pcs_catalog_set_current(request):
    """POST handler for the 'Make current' button.

    Renames the existing PCS lifecycle='current' Campaign to whatever
    target the operator selected on the banner. AI never auto-flips
    this — humans switch.
    """
    if request.method != 'POST':
        return _post_only_redirect(
            request, reverse('pcs:pcs_catalog'),
            action_label='Make current')
    target = (request.POST.get('name') or '').strip()
    from .services import (rename_pcs_current_campaign,
                           import_jlab_rucio_current_snapshot, ServiceError)
    try:
        result = rename_pcs_current_campaign(
            target,
            created_by=getattr(request.user, 'username', '') or 'operator',
        )
    except ServiceError as e:
        messages.error(request, f'Switch failed: {e}')
        return redirect(reverse('pcs:pcs_catalog'))
    if not result.get('changed'):
        messages.info(request, f"PCS current campaign already {target}.")
        return redirect(reverse('pcs:pcs_catalog'))
    log_epicprod_action(
        'web', 'campaign_set_current',
        subject_type='campaign', subject_key=result['name'],
        username=getattr(request.user, 'username', '') or 'operator',
        sublevel='high', live_default=True, previous=result.get('old_name', ''))
    # Pull the snapshot for the new current as part of the same click —
    # operator already consented by clicking 'Make current'; no point
    # making them hunt for 'Update from Rucio' next.
    try:
        snap = import_jlab_rucio_current_snapshot(
            created_by=getattr(request.user, 'username', '') or 'operator',
        )
        counts = ', '.join(f'{k}={v}' for k, v in snap['paths'].items())
        messages.success(
            request,
            f"PCS current: {result['old_name']} -> {result['name']}. "
            f"Snapshot pulled: {counts}. {len(snap['errors'])} errors.")
    except (ServiceError, OSError) as e:
        messages.warning(
            request,
            f"PCS current renamed to {result['name']} but snapshot pull "
            f"failed: {e}. Click 'Update from Rucio' to retry.")
    return redirect(reverse('pcs:pcs_catalog'))


@_login_required_flash
def pcs_catalog_set_last(request):
    """POST handler for 'Make last' button (Last tab selector).

    Sets the PCS lifecycle='last' Campaign to the named release and
    pulls its Rucio snapshot in the same click.
    """
    if request.method != 'POST':
        return _post_only_redirect(
            request, reverse('pcs:pcs_catalog') + '?lifecycle=last',
            action_label='Make last')
    target = (request.POST.get('name') or '').strip()
    from .services import (set_pcs_campaign_lifecycle,
                           import_jlab_rucio_current_snapshot, ServiceError)
    try:
        result = set_pcs_campaign_lifecycle(
            target, 'last',
            created_by=getattr(request.user, 'username', '') or 'operator')
    except ServiceError as e:
        messages.error(request, f'Make last failed: {e}')
        return redirect(reverse('pcs:pcs_catalog') + '?lifecycle=last')
    log_epicprod_action(
        'web', 'campaign_set_last',
        subject_type='campaign', subject_key=result['name'],
        username=getattr(request.user, 'username', '') or 'operator',
        sublevel='high', live_default=True)
    try:
        snap = import_jlab_rucio_current_snapshot(
            campaign_name=target,
            created_by=getattr(request.user, 'username', '') or 'operator')
        counts = ', '.join(f'{k}={v}' for k, v in snap['paths'].items())
        messages.success(
            request,
            f"PCS last set to {result['name']}. Snapshot: {counts}.")
    except (ServiceError, OSError) as e:
        messages.warning(
            request,
            f"PCS last set to {result['name']} but snapshot pull failed: {e}")
    return redirect(reverse('pcs:pcs_catalog') + '?lifecycle=last')


@_login_required_flash
def pcs_catalog_rucio_update(request):
    """No-JS POST fallback for the catalog 'Update from Rucio' button.

    The button's JavaScript posts to the /pcs/api/ endpoint (the external-safe
    trigger that survives the swf-remote proxy — see docs/EPICPROD_OPS_AGENT.md);
    this page-view handles the no-JavaScript case only and is reachable on the
    internal face. Both publish the same rucio_snapshot_update via
    services.rucio_snapshot_update_request. POST-only.
    See docs/EPICPROD_DATA_LINEAGE.md, docs/EPICPROD_OPS_AGENT.md.
    """
    if request.method != 'POST':
        return _post_only_redirect(
            request, reverse('pcs:pcs_catalog'),
            action_label='Update from Rucio')
    # Return to the tab the button lives on (last or producing) — the
    # URL is rebuilt server-side from the posted fields, never echoed.
    back = reverse('pcs:pcs_catalog')
    lifecycle = (request.POST.get('lifecycle') or '').strip()
    campaign = (request.POST.get('campaign') or '').strip()
    if lifecycle == 'producing' and campaign:
        back += f'?lifecycle=producing&campaign={campaign}'
    elif lifecycle == 'last':
        back += '?lifecycle=last'
    from .services import rucio_snapshot_update_request, ServiceError
    user = getattr(request.user, 'username', '') or 'rucio_snapshot'
    try:
        rucio_snapshot_update_request(created_by=user)
    except ServiceError as e:
        messages.error(request, e.detail)
        return redirect(back)
    messages.success(request, 'Rucio update queued — refreshing in the background.')
    return redirect(back)


@_login_required_flash
def pcs_catalog_evgen_update(request):
    """No-JS POST fallback for the catalog 'Update EVGEN from Rucio' button.

    The button's JavaScript posts to the /pcs/api/ endpoint (the external-safe
    trigger); this page-view handles the no-JavaScript case on the internal
    face. Both publish the same evgen_rucio_update via
    services.evgen_rucio_update_request. POST-only.
    See docs/EPICPROD_EVGEN_INPUTS.md, docs/EPICPROD_OPS_AGENT.md.
    """
    if request.method != 'POST':
        return _post_only_redirect(
            request, reverse('pcs:pcs_catalog'),
            action_label='Update EVGEN from Rucio')
    from .services import evgen_rucio_update_request, ServiceError
    user = getattr(request.user, 'username', '') or 'evgen_rucio'
    try:
        evgen_rucio_update_request(created_by=user)
    except ServiceError as e:
        messages.error(request, e.detail)
        return redirect(reverse('pcs:pcs_catalog'))
    messages.success(request, 'EVGEN update queued — refreshing in the background.')
    return redirect(reverse('pcs:pcs_catalog'))


@_login_required_flash
def pcs_catalog_questionnaire_match_update(request):
    """No-JS fallback for the catalog questionnaire-match cache button.

    The JavaScript path posts to /pcs/api/ and waits for the prod-ops
    questionnaire_match_ready event. This page-view only queues the same
    background agent work when JavaScript is unavailable.
    """
    if request.method != 'POST':
        return _post_only_redirect(
            request, reverse('pcs:pcs_catalog'),
            action_label='Update questionnaire matches')
    from .services import questionnaire_match_update_request, ServiceError
    user = getattr(request.user, 'username', '') or 'questionnaire_match'
    try:
        questionnaire_match_update_request(created_by=user)
    except ServiceError as e:
        messages.error(request, e.detail)
        return redirect(reverse('pcs:pcs_catalog'))
    messages.success(
        request,
        'Questionnaire match update queued — refreshing in the background.')
    return redirect(reverse('pcs:pcs_catalog'))


@_login_required_flash
def pcs_catalog_progress_refresh(request):
    """Refresh the cached current-campaign progress snapshot.

    This is intentionally a manual refresh path. The catalog page reads the
    cached snapshot from Campaign.data and does not query Rucio or scan PanDA on
    every page load.
    """
    target_url = reverse('pcs:pcs_catalog') + '?lifecycle=current&view=progress'
    if request.method != 'POST':
        return _post_only_redirect(
            request, target_url,
            action_label='Refresh progress')
    from .services import campaign_progress_refresh_request, ServiceError
    user = getattr(request.user, 'username', '') or 'progress_refresh'
    try:
        campaign_progress_refresh_request(created_by=user)
    except ServiceError as e:
        messages.error(request, e.detail)
        return redirect(target_url)
    messages.success(request, 'Progress refresh queued — updating in the background.')
    return redirect(target_url)


@_login_required_flash
def pcs_catalog_cache_refresh(request):
    """Manually rebuild cached current-campaign catalog/progress table HTML."""
    if request.method != 'POST':
        return _post_only_redirect(
            request, reverse('pcs:pcs_catalog'),
            action_label='Refresh catalog table')
    view = (request.POST.get('view') or 'catalog').strip()
    if view not in ('catalog', 'progress'):
        view = 'catalog'
    target_url = reverse('pcs:pcs_catalog') + '?lifecycle=current'
    if view == 'progress':
        target_url += '&view=progress'
    campaign = Campaign.objects.filter(lifecycle='current').order_by('name').first()
    if campaign is None:
        messages.error(request, 'No current campaign is available.')
        return redirect(target_url)
    progress_snapshot = None
    if view == 'progress':
        from .services import load_campaign_progress_snapshot
        progress_snapshot = load_campaign_progress_snapshot(campaign)
    rebuild_current_task_list_html_cache(
        campaign, view, progress_snapshot=progress_snapshot)
    return redirect(target_url)


def rucio_did_detail(request, scope, name):
    """Self-hosted Rucio DID detail — a live, read-only browser for any DID,
    since ePIC has no public Rucio webui. GET page-view → external-safe through
    the swf-remote proxy (no write, no redirect, no agent credential; reads use
    the public eicread userpass). Generic over DID type — input EVGEN and output
    RECO render identically; only the links into it differ. The file list loads
    on demand (rucio_did_files). A back-link to associated ProdTasks is a planned
    phase-1.5 add (reverse lookup over overrides['outputs'] / input DIDs).
    See docs/EPICPROD_DATA_LINEAGE.md."""
    from .services import fetch_jlab_rucio_did, ServiceError
    norm = '/' + name.lstrip('/')
    ctx = {'scope': scope, 'name': norm, 'name_url': norm.lstrip('/'),
           'did': f'{scope}:{norm}'}
    try:
        ctx['r'] = fetch_jlab_rucio_did(scope, norm)
    except ServiceError as e:
        ctx['error'] = e.detail
        return render(request, 'pcs/rucio_did_detail.html', ctx, status=e.status)
    # Provenance panel (docs/EPICPROD_DATA_LINEAGE.md): recorded and
    # convention-resolved links between produced data and EVGEN inputs.
    # This page is the live Rucio browser, so its one extra existence
    # check rides the same read; a failure shows in the panel, never
    # kills the page.
    from .services import data_provenance
    try:
        ctx['prov'] = data_provenance(f'{scope}:{norm}')
    except ServiceError as e:
        ctx['prov_error'] = e.detail
    return render(request, 'pcs/rucio_did_detail.html', ctx)


def rucio_did_files(request, scope, name):
    """On-demand JSON file list for the DID detail page (can be thousands)."""
    from .services import fetch_jlab_rucio_did_files, ServiceError
    if not request.user.is_authenticated:
        return JsonResponse({'error': 'Login required'}, status=403)
    try:
        files = fetch_jlab_rucio_did_files(scope, name)
    except ServiceError as e:
        return JsonResponse({'error': e.detail}, status=e.status)
    return JsonResponse({'files': files, 'count': len(files)})


def pcs_config_detail(request, label):
    """The physics-configuration page: identity (read-only — identity
    flows only through composition), associations (requestors, edited
    here with the required comment), editions across campaigns, and
    anchored requests. The PC's home; the plan and physics lists link
    into it."""
    from .physics_config import physics_config_key

    config = get_object_or_404(PhysicsConfig, label=label)
    editions = list(
        Dataset.objects.filter(physics_config=config)
        .select_related('campaign', 'simu_tag', 'reco_tag')
        .order_by('-composed_name', 'block_num', 'pk')
        .distinct('composed_name'))
    projection = services.pc_request_projection(editions)
    requests_seen = {}
    for reqs in projection.values():
        for req in reqs:
            requests_seen[req.pk] = req
    known_labels = sorted({
        value for values in PhysicsConfig.objects.exclude(requestors=[])
        .values_list('requestors', flat=True) for value in values})
    history = list(reversed(
        ((config.metadata or {}).get('requestors') or {})
        .get('history') or []))[:10]
    return render(request, 'pcs/config_detail.html', {
        'config': config,
        'editions': editions,
        'requests': sorted(requests_seen.values(), key=lambda r: r.pk),
        'known_labels': known_labels,
        'history': history,
    })


def _plan_delivery_embed(campaign, state):
    """The campaign's last-7-day arrivals quilt and accrued-files stack
    for the plan page (CAMPAIGN_DELIVERY.md surface 1): the factorized
    snapper embed as a cached product, built unfiltered from the
    delivery component's snaps and pruned per request to the active
    plan filters; the click-through opens the campaign Time history
    carrying the same filters. None when nothing matches the window or
    the build fails (failure is logged, never fatal to the plan
    page)."""
    import logging
    from datetime import timedelta
    from urllib.parse import quote, urlencode

    from django.utils import timezone as dj_timezone

    from monitor_app.cached_product import get_product
    from snapper_ai.embed import embed_context

    tag = campaign.name.replace('.', '_')

    def build():
        now = dj_timezone.now()
        ctx = embed_context(
            'epicprod', now - timedelta(days=7), now,
            families=(
                f'Arrivals {campaign.name} files',
                # Inline panel spec: the per-PC accrued-files curves,
                # stacked — the accrual companion to the quilt.
                {'name': f'plan-accrued-{campaign.name}',
                 'title': f'Accrued {campaign.name} files',
                 'prefixes': [f'dlvpcf_{tag}_'],
                 'empty_note': 'No accrued files in this window',
                 'stacked': True},
            ),
            snap_components=('delivery',))
        if ctx.get('error'):
            raise RuntimeError(ctx['error'])
        for panel in ctx['data']['panels']:
            if str(panel.get('name', '')).startswith('Arrivals'):
                panel['empty_note'] = 'No arrivals in this window'
        ctx['report_focus_slug'] = 'campaign'
        ctx['report_query'] += f'&campaign={quote(campaign.name)}'
        ctx['panel_px'] = 220
        return ctx

    try:
        product = get_product(
            f'snapper_embed:v3:pcs_plan:{campaign.name}', build,
            ttl_seconds=300)
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).error(
            'plan delivery embed failed for %s: %s', campaign.name, exc)
        return None
    ctx = product.get('value')
    if not ctx or not ctx.get('has_points'):
        return None
    echo = state.get('filter_echo') or {}
    if echo:
        # Prune the cached full-campaign context to the filtered slice
        # (the value is deserialized fresh per request, so in-place is
        # safe) and carry the filters into the Time-history link.
        pc_set = {r['pc_label'] for r in state['rows'] if r['pc_label']}
        prefixes = tuple(f'{p}{tag}_'
                         for p in ('dlvq_', 'dlvqf_', 'dlvpc_', 'dlvpcf_'))

        def keep(curve_id):
            for prefix in prefixes:
                if curve_id.startswith(prefix):
                    return curve_id[len(prefix):] in pc_set
            return False

        data = ctx['data']
        data['curves'] = {curve_id: curve
                          for curve_id, curve in data['curves'].items()
                          if keep(curve_id)}
        for panel in data['panels']:
            panel['ids'] = [curve_id for curve_id in panel['ids']
                            if keep(curve_id)]
        ctx['has_points'] = any(curve['points']
                                for curve in data['curves'].values())
        ctx['report_query'] += '&' + urlencode(echo)
        if not ctx['has_points']:
            return None
    return ctx


PLAN_STATUS_SLUGS = (('complete', 'complete'),
                     ('below-target', 'below target'),
                     ('not-started', 'not started'),
                     ('no-target', 'no target'))

# The plan's filter parameters, in facet-row order — the vocabulary the
# plan page, its snapper embed, and the campaign Time history filter
# carry-through all share.
PLAN_FILTER_PARAMS = ('requestor', 'process', 'generator', 'beam', 'q2',
                      'sample', 'nev', 'priority', 'status')

# Reader-facing wording for the campaign-plan recommendation values
# (the internal identifiers stay stable in payloads and the executor).
CAMPAIGN_PLAN_DISPO_LABELS = {
    'include': 'include',
    'defer': 'defer',
    'retire': 'retire',
}


def _apply_plan_filters(rows, query, skip=()):
    """Apply the plan filters carried in ``query`` to ``rows``, skipping
    the parameters named in ``skip`` — each facet row counts over the
    slice made by every OTHER active filter (the self-excluded
    convention), so its counts are true within the current slice while
    switching within the facet stays possible."""
    def val(key):
        return '' if key in skip else (query.get(key) or '').strip()

    for key in ('process', 'generator', 'beam', 'q2', 'sample'):
        value = val(key)
        if value:
            rows = [r for r in rows if r[key] == value]
    # Requestor is multi-membership: a row matches when it carries the
    # label; 'Unassigned' matches the empty list.
    requestor = val('requestor')
    if requestor == 'Unassigned':
        rows = [r for r in rows if not r['requestors']]
    elif requestor:
        rows = [r for r in rows if requestor in r['requestors']]
    nev = val('nev')
    if nev == 'specified':
        rows = [r for r in rows if r['expected_events'] is not None]
    elif nev == 'unspecified':
        rows = [r for r in rows if r['expected_events'] is None]
    priority = val('priority')
    if priority == 'none':
        rows = [r for r in rows if r['priority'] is None]
    elif priority.isdigit():
        rows = [r for r in rows if r['priority'] == int(priority)]
    status_label = dict(PLAN_STATUS_SLUGS).get(val('status'))
    if status_label:
        rows = [r for r in rows if r['status'] == status_label]
    return rows


def _campaign_plan_state(campaign, query, pc_view):
    """Rows and filter state of the campaign plan — the single source
    for the plan page and the snapper campaign-view filter
    carry-through. Builds the per-head rows with the completion join,
    collapses to one row per PC when ``pc_view``, applies every plan
    filter in ``query`` (a plain mapping), and names the active
    filters."""
    heads = []
    requested = {}
    if campaign is not None:
        heads = list(
            Dataset.objects.filter(campaign=campaign)
            .select_related('physics_tag', 'evgen_tag', 'background_tag',
                            'physics_config')
            .order_by('composed_name', 'block_num', 'pk')
            .distinct('composed_name'))
        # Requested-tier fallback via the PC-request anchor (the
        # CAMPAIGN_DELIVERY.md denominator chain: included -> requested
        # -> absent). Several requests can anchor to one configuration;
        # the largest single ask stands in, never a sum of possible
        # duplicates.
        projection = services.pc_request_projection(heads)
        for name, reqs in projection.items():
            values = [r.nevents for r in reqs if r.nevents]
            if values:
                requested[name] = max(values)

    # Completion join (CAMPAIGN_DELIVERY.md, Completion): per-PC rows
    # from the shared campaign-completion cached product — the panel's
    # source, never a build in the request path. Empty for campaigns
    # outside the product (future, not yet producing).
    completion_by_pc = {}
    if campaign is not None:
        from monitor_app.cached_product import get_product
        from swf_epicprod.analytics.completion import completion_product_value
        product = get_product('prod_hub_campaign_completion',
                              completion_product_value, ttl_seconds=600)
        entry = next((e for e in (product['value'] or [])
                      if e.get('campaign') == campaign.name), None)
        if entry:
            completion_by_pc = {r['pc']: r for r in (entry.get('pcs') or [])}

    gen_case = _generator_display_case()
    rows = []
    for head in heads:
        params = (head.physics_tag.parameters or {}) if head.physics_tag_id \
            else {}
        evgen_params = (head.evgen_tag.parameters or {}) if head.evgen_tag_id \
            else {}
        generator = evgen_params.get('generator', '')
        generator = gen_case.get(generator, generator)
        version = evgen_params.get('generator_version', '')
        be = str(params.get('beam_energy_electron', '') or '')
        bh = str(params.get('beam_energy_hadron', '') or '')
        if be.upper() == 'N/A':
            be = ''
        if bh.upper() == 'N/A':
            bh = ''
        comp = (completion_by_pc.get(head.physics_config.label)
                if head.physics_config_id else None) or {}
        rows.append({
            'priority': comp.get('priority'),
            'delivered_events': comp.get('delivered_events'),
            'completion_pct': (round(100 * comp['completion'])
                               if comp.get('completion') is not None
                               else None),
            'status': comp.get('status', ''),
            'name': head.composed_name,
            'physics': head.physics_tag.tag_label if head.physics_tag_id
                       else '',
            'process': params.get('process', ''),
            'beam': f'{be}x{bh}' if be and bh else (be or bh),
            'q2': params.get('q2_range', ''),
            'generator': ' '.join(p for p in (generator, version) if p),
            'sample': head.sample_name,
            'pc_label': (head.physics_config.label
                         if head.physics_config_id else ''),
            'requestors': (list(head.physics_config.requestors or [])
                           if head.physics_config_id else []),
            'propagation': head.propagation,
            'expected_events': head.expected_events,
            'expected_source': head.expected_events_source,
            'requested_events': requested.get(head.composed_name),
        })
    if pc_view:
        # One row per physics configuration — the completion table the
        # home panel's counts link into. The representative edition is
        # the first head carrying a target, else the first head (the
        # pc_targets rule); heads without a configuration are outside
        # the completion record and are not shown in this view.
        by_pc = {}
        for r in rows:
            label = r['pc_label']
            if not label:
                continue
            keep = by_pc.get(label)
            if keep is None or (keep['expected_events'] is None
                                and r['expected_events'] is not None):
                by_pc[label] = r
        rows = list(by_pc.values())

    rows.sort(key=lambda r: (
        (int(r['physics'][1:]) if r['physics'][1:].isdigit() else 0)
        if r['physics'] else 0, r['sample']))

    rows_all = rows
    filters = {key: (query.get(key) or '').strip()
               for key in ('process', 'generator', 'beam', 'q2', 'sample')}
    requestor_filter = (query.get('requestor') or '').strip()
    nev = (query.get('nev') or '').strip()
    priority_filter = (query.get('priority') or '').strip()
    status_filter = (query.get('status') or '').strip()
    status_label = dict(PLAN_STATUS_SLUGS).get(status_filter)
    rows = _apply_plan_filters(rows_all, query)

    # The named active filters (the page's Active-filters line and the
    # Time-history carry-through statement) and their parameter echo
    # (the query fragment that reproduces this slice).
    active_filters = []
    filter_echo = {}
    if requestor_filter:
        active_filters.append(('Requestor', requestor_filter))
        filter_echo['requestor'] = requestor_filter
    for key, label in (('process', 'Process'), ('generator', 'Generator'),
                       ('beam', 'Beam'), ('q2', 'Q²'), ('sample', 'Sample')):
        if filters[key]:
            active_filters.append((label, filters[key]))
            filter_echo[key] = filters[key]
    if nev:
        active_filters.append(('Target events', nev))
        filter_echo['nev'] = nev
    if priority_filter:
        active_filters.append(('Priority', priority_filter))
        filter_echo['priority'] = priority_filter
    if status_label:
        active_filters.append(('Status', status_label))
        filter_echo['status'] = status_filter

    return {'rows_all': rows_all, 'rows': rows, 'filters': filters,
            'requestor_filter': requestor_filter, 'nev': nev,
            'priority_filter': priority_filter,
            'status_filter': status_filter, 'status_label': status_label,
            'active_filters': active_filters, 'filter_echo': filter_echo,
            'has_completion': bool(completion_by_pc)}


def campaign_plan_pc_filter(campaign_name, query):
    """The snapper campaign view's filter carry-through: resolve the
    plan filter parameters in ``query`` against ``campaign_name`` and
    return ``(active_filters, pc label set)`` — or ``(None, None)``
    when no plan filter is active or the campaign is unknown."""
    if not any((query.get(key) or '').strip()
               for key in PLAN_FILTER_PARAMS):
        return None, None
    campaign = Campaign.objects.filter(name=campaign_name).first()
    if campaign is None:
        return None, None
    state = _campaign_plan_state(campaign, query, pc_view=True)
    if not state['active_filters']:
        return None, None
    return (state['active_filters'],
            {r['pc_label'] for r in state['rows'] if r['pc_label']})


def _campaign_assembly_context(campaign):
    """The assembly (proposal-build) context for a future campaign with
    no editions (CONTINUOUS_PRODUCTION.md, Campaign assembly): the
    union of pending campaign-plan proposals and approved plan entries,
    one row per physics configuration. None when neither exists."""
    from ai.models import Proposal

    plan = services.campaign_plan_get(campaign.name)
    pending = {row.subject_key: row for row in Proposal.objects.filter(
        action='campaign_plan', counterpart_key=campaign.name,
        status='proposed')}
    # The latest executed proposal per configuration backs the in-place
    # Undo on approved rows.
    executed = {}
    for prow in Proposal.objects.filter(
            action='campaign_plan', counterpart_key=campaign.name,
            status='executed').order_by('decided_at'):
        executed[prow.subject_key] = prow
    # Denied configurations stay visible so a denial can be undone.
    denied = {}
    for prow in Proposal.objects.filter(
            action='campaign_plan', counterpart_key=campaign.name,
            status='denied').order_by('decided_at'):
        denied[prow.subject_key] = prow
    labels = sorted(set(plan) | set(pending) | set(denied))
    if not labels:
        return None
    rows = []
    counts = {'proposed': 0, 'approved': 0, 'denied': 0}
    by_disposition = {}
    for label in labels:
        proposal = pending.get(label)
        if proposal is None and label not in plan:
            drow = denied[label]
            payload = drow.payload or {}
            rows.append({
                'pc': label,
                'state': 'denied',
                'proposal_id': None,
                'ref': drow.ref,
                'undo_id': drow.pk,
                'disposition': payload.get('disposition', ''),
                'disposition_label': '',
                'target_events': payload.get('target_events'),
                'priority': payload.get('priority'),
                'evidence': payload.get('evidence', ''),
            })
            counts['denied'] += 1
            continue
        if proposal is not None:
            payload = proposal.payload or {}
            row = {
                'pc': label,
                'state': 'proposed',
                'proposal_id': proposal.pk,
                'ref': proposal.ref,
                'disposition': payload.get('disposition', ''),
                'target_events': payload.get('target_events'),
                'priority': payload.get('priority'),
                'evidence': payload.get('evidence', ''),
            }
            counts['proposed'] += 1
        else:
            entry = plan[label]
            done = executed.get(label)
            row = {
                'pc': label,
                'state': 'approved',
                'proposal_id': None,
                'ref': done.ref if done else '',
                'undo_id': done.pk if done else None,
                'disposition': entry.get('disposition', ''),
                'target_events': entry.get('target_events'),
                'priority': entry.get('priority'),
                'evidence': entry.get('evidence', ''),
            }
            counts['approved'] += 1
        by_disposition[row['disposition']] = (
            by_disposition.get(row['disposition'], 0) + 1)
        rows.append(row)
    rows.sort(key=lambda r: (r['priority'] if r['priority'] is not None
                             else 99, r['pc']))
    return {
        'rows': rows,
        'total': len(rows),
        'proposed': counts['proposed'],
        'approved': counts['approved'],
        'denied': counts['denied'],
        'by_disposition': sorted(by_disposition.items()),
    }


def pcs_campaign_plan(request):
    """The campaign plan list (CAMPAIGN_DELIVERY.md surface 1): one
    active or future campaign's physics configurations with the
    campaign-included target-events column — filled where set, visibly
    missing where not. The curation surface for the delivery
    denominator: per-row entry saved in bulk through the
    expected-events REST endpoint with one required comment. A future
    campaign with no editions renders the assembly (proposal-build)
    view instead (CONTINUOUS_PRODUCTION.md, Campaign assembly).
    Read-open; deciding requires login.
    """
    plan_campaigns = list(
        Campaign.objects.filter(lifecycle__in=('current', 'future'))
        .order_by('-name'))
    selected_name = (request.GET.get('campaign') or '').strip()
    campaign = None
    if selected_name:
        campaign = Campaign.objects.filter(name=selected_name).first()
    if campaign is None:
        campaign = next((c for c in plan_campaigns
                         if c.lifecycle == 'current'), None)
    if campaign is None and plan_campaigns:
        campaign = plan_campaigns[0]
    if campaign is not None and campaign not in plan_campaigns:
        plan_campaigns.append(campaign)

    view_mode = 'pc' if (request.GET.get('view') or '') == 'pc' else 'edition'
    state = _campaign_plan_state(campaign, request.GET, view_mode == 'pc')

    # Assembly view (CONTINUOUS_PRODUCTION.md, Campaign assembly): a
    # future campaign with no editions renders its plan build. The full
    # filter machinery carries over — identity attributes join from the
    # current campaign's PC spine, the standard plan filters apply, and
    # the assembly-only recommendation and review-state axes join them.
    assembly = None
    dispo_filter = astate_filter = ''
    if campaign is not None and not state['rows_all']:
        assembly = _campaign_assembly_context(campaign)
    if assembly:
        source = next((c for c in plan_campaigns
                       if c.lifecycle == 'current' and c != campaign), None)
        attr_map = {}
        if source is not None:
            attr_state = _campaign_plan_state(source, {}, True)
            attr_map = {r['pc_label']: r for r in attr_state['rows_all']}
        for row in assembly['rows']:
            attrs = attr_map.get(row['pc'], {})
            for key in ('physics', 'process', 'beam', 'q2', 'generator',
                        'sample'):
                row[key] = attrs.get(key, '')
            row['requestors'] = attrs.get('requestors') or []
            row['pc_label'] = row['pc']
            row['expected_events'] = row['target_events']
            row['status'] = ''
            row['disposition_label'] = CAMPAIGN_PLAN_DISPO_LABELS.get(
                row['disposition'], row['disposition'])
        arows = _apply_plan_filters(assembly['rows'], request.GET)
        dispo_filter = (request.GET.get('dispo') or '').strip()
        astate_filter = (request.GET.get('astate') or '').strip()
        if dispo_filter:
            arows = [r for r in arows if r['disposition'] == dispo_filter]
        if astate_filter:
            arows = [r for r in arows if r['state'] == astate_filter]
        assembly['rows_filtered'] = arows
        assembly['shown'] = len(arows)
        assembly['by_disposition'] = [
            (CAMPAIGN_PLAN_DISPO_LABELS.get(d, d), n)
            for d, n in assembly['by_disposition']]
        state = dict(state)
        state['rows_all'] = assembly['rows']
        state['rows'] = arows
        state['has_completion'] = True
        active = list(state['active_filters'])
        if dispo_filter:
            active.append(('Disposition',
                           CAMPAIGN_PLAN_DISPO_LABELS.get(dispo_filter,
                                                          dispo_filter)))
        if astate_filter:
            active.append(('State', astate_filter))
        state['active_filters'] = active

    rows_all = state['rows_all']
    rows = state['rows']
    filters = state['filters']
    requestor_filter = state['requestor_filter']
    nev = state['nev']
    priority_filter = state['priority_filter']
    status_filter = state['status_filter']

    with_target = sum(1 for r in rows_all
                      if r['expected_events'] is not None)
    target_total = sum(r['expected_events'] for r in rows_all
                       if r['expected_events'] is not None)

    def url_with(**updates):
        params = request.GET.copy()
        for key, value in updates.items():
            if value:
                params[key] = value
            else:
                params.pop(key, None)
        encoded = params.urlencode()
        return f'{request.path}?{encoded}' if encoded else request.path

    def facet(param):
        # Counts over the slice made by every OTHER active filter, so
        # each count is true within the current slice; the active value
        # stays listed even at zero.
        counts = {}
        for r in _apply_plan_filters(rows_all, request.GET, skip=(param,)):
            value = r[param]
            if value:
                counts[value] = counts.get(value, 0) + 1
        if filters[param] and filters[param] not in counts:
            counts[filters[param]] = 0
        return {'items': [{'value': v, 'count': n,
                           'url': url_with(**{param: v}),
                           'active': filters[param] == v}
                          for v, n in sorted(counts.items())],
                'all_url': url_with(**{param: ''}),
                'all_active': not filters[param]}

    requestor_counts = {}
    unassigned_count = 0
    for r in _apply_plan_filters(rows_all, request.GET,
                                 skip=('requestor',)):
        if not r['requestors']:
            unassigned_count += 1
        for label in r['requestors']:
            requestor_counts[label] = requestor_counts.get(label, 0) + 1
    requestor_items = [
        {'value': label, 'count': count,
         'url': url_with(requestor=label),
         'active': requestor_filter == label}
        for label, count in sorted(requestor_counts.items())]
    if unassigned_count:
        requestor_items.append(
            {'value': 'Unassigned', 'count': unassigned_count,
             'url': url_with(requestor='Unassigned'),
             'active': requestor_filter == 'Unassigned'})
    facet_rows = [
        ('Requestor', {'items': requestor_items,
                       'all_url': url_with(requestor=''),
                       'all_active': not requestor_filter}),
        ('Process', facet('process')),
        ('Generator', facet('generator')),
        ('Beam', facet('beam')),
        ('Q²', facet('q2')),
        ('Sample', facet('sample')),
    ]
    nev_base = _apply_plan_filters(rows_all, request.GET, skip=('nev',))
    specified_count = sum(
        1 for r in nev_base if r['expected_events'] is not None)
    facet_rows.append(('Target events', {
        'items': [
            {'value': 'specified', 'count': specified_count,
             'url': url_with(nev='specified'), 'active': nev == 'specified'},
            {'value': 'unspecified',
             'count': len(nev_base) - specified_count,
             'url': url_with(nev='unspecified'),
             'active': nev == 'unspecified'},
        ],
        'all_url': url_with(nev=''),
        'all_active': not nev,
    }))
    # Priority and Status facets exist only where the completion record
    # covers the campaign (current or producing).
    if state['has_completion']:
        priority_counts = {}
        no_priority_count = 0
        for r in _apply_plan_filters(rows_all, request.GET,
                                     skip=('priority',)):
            if r['priority'] is None:
                no_priority_count += 1
            else:
                priority_counts[r['priority']] = (
                    priority_counts.get(r['priority'], 0) + 1)
        if (priority_filter.isdigit()
                and int(priority_filter) not in priority_counts):
            priority_counts[int(priority_filter)] = 0
        priority_items = [
            {'value': str(p), 'count': n, 'url': url_with(priority=str(p)),
             'active': priority_filter == str(p)}
            for p, n in sorted(priority_counts.items())]
        if no_priority_count or priority_filter == 'none':
            priority_items.append(
                {'value': 'none', 'count': no_priority_count,
                 'url': url_with(priority='none'),
                 'active': priority_filter == 'none'})
        facet_rows.append(('Priority', {
            'items': priority_items,
            'all_url': url_with(priority=''),
            'all_active': not priority_filter}))
        if assembly is None:
            status_base = _apply_plan_filters(rows_all, request.GET,
                                              skip=('status',))
            status_counts = {label: sum(1 for r in status_base
                                        if r['status'] == label)
                             for _slug, label in PLAN_STATUS_SLUGS}
            facet_rows.append(('Status', {
                'items': [
                    {'value': label, 'count': status_counts[label],
                     'url': url_with(status=slug),
                     'active': status_filter == slug}
                    for slug, label in PLAN_STATUS_SLUGS
                    if status_counts[label] or status_filter == slug],
                'all_url': url_with(status=''),
                'all_active': not status_filter}))
    if assembly is not None:
        # The assembly axes, self-excluded like every other facet: each
        # counts over the rows the OTHER filters leave.
        def _assembly_base(skip_dispo=False, skip_astate=False):
            base = _apply_plan_filters(rows_all, request.GET)
            if dispo_filter and not skip_dispo:
                base = [r for r in base
                        if r['disposition'] == dispo_filter]
            if astate_filter and not skip_astate:
                base = [r for r in base if r['state'] == astate_filter]
            return base

        dispo_base = _assembly_base(skip_dispo=True)
        facet_rows.append(('Disposition', {
            'items': [
                {'value': CAMPAIGN_PLAN_DISPO_LABELS[value],
                 'count': sum(1 for r in dispo_base
                              if r['disposition'] == value),
                 'url': url_with(dispo=value),
                 'active': dispo_filter == value}
                for value in CAMPAIGN_PLAN_DISPO_LABELS
                if sum(1 for r in dispo_base
                       if r['disposition'] == value)
                or dispo_filter == value],
            'all_url': url_with(dispo=''),
            'all_active': not dispo_filter}))
        astate_base = _assembly_base(skip_astate=True)
        facet_rows.append(('State', {
            'items': [
                {'value': value,
                 'count': sum(1 for r in astate_base
                              if r['state'] == value),
                 'url': url_with(astate=value),
                 'active': astate_filter == value}
                for value in ('proposed', 'approved')
                if sum(1 for r in astate_base if r['state'] == value)
                or astate_filter == value],
            'all_url': url_with(astate=''),
            'all_active': not astate_filter}))

    return render(request, 'pcs/campaign_plan.html', {
        'campaign': campaign,
        'assembly': assembly,
        'active_filters': state['active_filters'],
        'view_mode': view_mode,
        'view_edition_url': url_with(view=''),
        'view_pc_url': url_with(view='pc'),
        'plan_campaigns': plan_campaigns,
        'snapper_embed': (_plan_delivery_embed(campaign, state)
                          if campaign is not None else None),
        'rows': rows,
        'total': len(rows_all),
        'shown': len(rows),
        'facet_rows': facet_rows,
        'clear_url': url_with(process='', generator='', beam='', q2='',
                              sample='', requestor='', nev='', priority='',
                              status='', dispo='', astate=''),
        'with_target': with_target,
        'without_target': len(rows_all) - with_target,
        'target_total': target_total,
    })


def _build_physics_rows(years):
    """The physics-configuration assembly — the expensive whole-catalog
    build behind the physics page and its cached product: campaigns for
    the year selection and one plain-data row per configuration with
    its editions along the campaign axis."""
    from .physics_config import group_editions

    # Newest first, left to right — the eye lands on the most recent.
    campaigns = sorted(
        (c.name for c in Campaign.objects.all()
         if years == 'all' or c.name.startswith(years[2:] + '.')),
        key=lambda n: _version_tuple(n) or (0,), reverse=True)

    heads = list(
        Dataset.objects.filter(campaign__name__in=campaigns)
        .select_related('physics_tag', 'evgen_tag', 'background_tag',
                        'campaign', 'physics_config')
        .order_by('composed_name', 'block_num', 'pk')
        .distinct('composed_name'))
    groups = group_editions(heads)
    requests_by_anchor = services.pc_anchored_requests()

    # One working task per identity decides the edition's state; sibling
    # output records don't override it. Matched requests ride along.
    task_status = {}
    task_requests = {}
    for name, status, overrides in (
            ProdTask.objects.filter(campaign__name__in=campaigns)
            .values_list('dataset__composed_name', 'status', 'overrides')):
        if name not in task_status or task_status[name] == 'past_output':
            task_status[name] = status
        for match in (overrides or {}).get('questionnaire_matches') or []:
            qid = match.get('questionnaire_id') if isinstance(match, dict) else None
            if isinstance(qid, int) or str(qid).isdigit():
                task_requests.setdefault(name, set()).add(int(qid))

    # Produced data per identity, summed over ALL its physical rows —
    # the head row alone undercounts multi-row identities.
    produced = {
        row['composed_name']: row
        for row in Dataset.objects.filter(campaign__name__in=campaigns)
        .values('composed_name')
        .annotate(n_datasets=Count('id'), files=Sum('file_count'),
                  size=Sum('data_size'))
    }

    gen_case = _generator_display_case()
    rows = []
    for key, group in groups.items():
        head, detail = group['editions'][0]
        params = (head.physics_tag.parameters or {}) if head.physics_tag else {}
        evgen = detail['evgen']
        generator = gen_case.get(evgen[0], evgen[0]) if evgen else ''
        species = params.get('beam_species', '')
        gen_display = ((' '.join(part for part in (generator, evgen[1]) if part)
                        + (f' {species}' if species else '')
                        + (' noRad' if evgen[2] == 'off' else '')
                        + (' Rad' if evgen[2] == 'on' else ''))
                       if evgen else '')
        be = str(params.get('beam_energy_electron', '') or '')
        bh = str(params.get('beam_energy_hadron', '') or '')
        if be.upper() == 'N/A':
            be = ''
        if bh.upper() == 'N/A':
            bh = ''
        beam = f'{be}x{bh}' if be and bh else (be or bh)
        row = {
            'physics': head.physics_tag.tag_label if head.physics_tag else '',
            'pc_label': (head.physics_config.label
                         if head.physics_config_id else ''),
            'process': params.get('process', ''),
            'beam': beam,
            'species': species,
            'q2': params.get('q2_range', ''),
            'generator': generator,
            'gen_display': gen_display,
            'sample': detail['sample'],
            'editions': {},
        }
        row['requests'] = sorted(set().union(*(
            task_requests.get(d.composed_name, set())
            for d, _ in group['editions'])))
        # PC-anchored production requests: a request whose anchor is any
        # of this configuration's editions belongs to the configuration.
        # Plain data — the row set is a cached product.
        row['prod_requests'] = [
            {'pk': req.pk, 'requestor': req.requestor,
             'nevents': req.nevents,
             'anchor': (req.data or {}).get('physics_config_anchor', '')}
            for req in sorted(
                {req.pk: req for d, _ in group['editions']
                 for req in requests_by_anchor.get(d.composed_name, ())
                 }.values(), key=lambda r: r.pk)]
        for dataset, edition_detail in group['editions']:
            camp = dataset.campaign.name if dataset.campaign_id else ''
            data = produced.get(dataset.composed_name) or {}
            row['editions'][camp] = {
                'name': dataset.composed_name,
                'status': task_status.get(dataset.composed_name, ''),
                'n_datasets': data.get('n_datasets') or 0,
                'files': data.get('files') or 0,
                'size': data.get('size') or 0,
                'propagation': dataset.propagation,
                'replaced_by': dataset.replaced_by,
                'proposal': bool((dataset.metadata or {}).get('proposal')),
            }
        # Template-friendly: one cell per campaign column, aligned.
        row['cells'] = [row['editions'].get(c) for c in campaigns]
        row['search_blob'] = ' '.join(
            [row['physics'], row['process'], row['beam'], row['species'],
             row['q2'], row['gen_display'], row['sample']]
            + [e['name'] for e in row['editions'].values()]).lower()
        rows.append(row)
    return {'campaigns': campaigns, 'rows': rows, 'total': len(groups)}


def pcs_physics_configs(request):
    """The physics-configuration view (CAMPAIGN_CONTINUUM.md): physics
    first, fulfillment through time. One row per physics configuration,
    its editions along the campaign axis — presentation only, no
    compose, no editing; anything actionable cross-links to the
    catalog. Read-open. The whole-catalog assembly serves as a cached
    product (swf-monitor docs/CACHED_PRODUCTS.md): the stored result
    immediately, staleness rebuilding behind, Update rebuilding now.
    Filters, facets, and totals are cheap projections applied to the
    cached rows per request.
    """
    from monitor_app.cached_product import get_product

    def url_with(**updates):
        params = request.GET.copy()
        params.pop('refresh', None)
        for key, value in updates.items():
            if value:
                params[key] = value
            else:
                params.pop(key, None)
        encoded = params.urlencode()
        return f'{request.path}?{encoded}' if encoded else request.path

    years = (request.GET.get('years') or '2026').strip()
    if years not in ('2026', '2025', 'all'):
        years = '2026'
    refresh = request.GET.get('refresh') == '1'
    product = get_product(f'pcs_physics:v1:{years}',
                          lambda: _build_physics_rows(years),
                          ttl_seconds=300, refresh=refresh)
    cached = product['value'] or {'campaigns': [], 'rows': [], 'total': 0}
    campaigns = cached['campaigns']
    total_groups = cached.get('total') or 0

    filters = {key: (request.GET.get(key) or '').strip()
               for key in ('process', 'generator', 'beam', 'species', 'q2',
                           'sample')}
    q = (request.GET.get('q') or '').strip().lower()
    produced_in = (request.GET.get('produced') or '').strip()

    rows_all = cached['rows']
    rows = rows_all
    for key, value in filters.items():
        if value:
            rows = [r for r in rows if r[key] == value]
    if q:
        rows = [r for r in rows if q in r['search_blob']]
    if produced_in in campaigns:
        produced_index = campaigns.index(produced_in)
        rows = [r for r in rows
                if r['cells'][produced_index]
                and r['cells'][produced_index]['files']]
    matched = (request.GET.get('matched') or '').strip()
    if matched == 'matched':
        rows = [r for r in rows if r['requests']]
    elif matched == 'unmatched':
        rows = [r for r in rows if not r['requests']]

    # Per-campaign fulfillment totals over the filtered set — the
    # dataset/file/volume picture the view exists to surface.
    campaign_totals = []
    for index, camp in enumerate(campaigns):
        total = {'configs': 0, 'produced': 0, 'n_datasets': 0,
                 'files': 0, 'size': 0}
        for r in rows:
            cell = r['cells'][index]
            if not cell:
                continue
            total['configs'] += 1
            total['n_datasets'] += cell['n_datasets']
            if cell['files']:
                total['produced'] += 1
                total['files'] += cell['files']
                total['size'] += cell['size']
        campaign_totals.append(total)

    def facet(param):
        counts = {}
        for r in rows_all:
            value = r[param]
            if value:
                counts[value] = counts.get(value, 0) + 1
        return {'param': param,
                'items': [{'value': v, 'count': n,
                           'url': url_with(**{param: v}),
                           'active': filters[param] == v}
                          for v, n in sorted(counts.items())],
                'all_url': url_with(**{param: ''}),
                'all_active': not filters[param]}

    facet_rows = [
        ('Process', facet('process')),
        ('Generator', facet('generator')),
        ('Beam', facet('beam')),
        ('Species', facet('species')),
        ('Q²', facet('q2')),
        ('Sample', facet('sample')),
    ]
    produced_counts = {
        camp: sum(1 for r in rows_all
                  if r['cells'][i] and r['cells'][i]['files'])
        for i, camp in enumerate(campaigns)
    }
    matched_count = sum(1 for r in rows_all if r['requests'])
    facet_rows.append(('Request', {
        'param': 'matched',
        'items': [
            {'value': 'matched', 'count': matched_count,
             'url': url_with(matched='matched'),
             'active': matched == 'matched'},
            {'value': 'unmatched', 'count': len(rows_all) - matched_count,
             'url': url_with(matched='unmatched'),
             'active': matched == 'unmatched'},
        ],
        'all_url': url_with(matched=''),
        'all_active': not matched,
    }))
    facet_rows.append(('Has data', {
        'param': 'produced',
        'items': [{'value': camp, 'count': n,
                   'url': url_with(produced=camp),
                   'active': produced_in == camp}
                  for camp, n in produced_counts.items() if n],
        'all_url': url_with(produced=''),
        'all_active': not produced_in,
    }))

    # Default order: physics tag ascending (numeric within the p prefix).
    def _physics_sort_key(row):
        label = row['physics']
        number = (int(label[1:]) if label[1:].isdigit() else 0) if label else 0
        return (number, row['sample'])
    rows.sort(key=_physics_sort_key)
    return render(request, 'pcs/physics_configs.html', {
        'rows': rows,
        'campaigns': campaigns,
        'campaign_totals': campaign_totals,
        'years': years,
        'years_urls': {y: url_with(years=y) for y in ('2026', '2025', 'all')},
        'facet_rows': facet_rows,
        'q': request.GET.get('q', ''),
        'clear_url': request.path,
        'total': total_groups,
        'shown': len(rows),
        'product_built_at_text': (
            product['built_at'].astimezone(_ET)
            .strftime('%Y-%m-%d %H:%M ET')
            if product['built_at'] else ''),
        'product_refreshing': product['refreshing'],
        'product_update_url': url_with(refresh='1'),
    })


def _build_composer_configs():
    """The composer's configuration list — the whole-catalog assembly
    behind the request composer, served as a cached product."""
    from .physics_config import group_editions
    select = ('physics_tag', 'evgen_tag', 'background_tag', 'campaign')
    heads = list(Dataset.objects.select_related(*select)
                 .order_by('composed_name', 'block_num', 'pk')
                 .distinct('composed_name'))
    groups = group_editions(heads)
    produced = {
        row['composed_name']: row['files'] or 0
        for row in Dataset.objects.values('composed_name')
        .annotate(files=Sum('file_count'))
    }
    configs = []
    gen_case = _generator_display_case()
    for key, group in groups.items():
        head, detail = group['editions'][0]
        params = (head.physics_tag.parameters or {}) if head.physics_tag else {}
        evgen = detail['evgen']
        be = str(params.get('beam_energy_electron', '') or '')
        bh = str(params.get('beam_energy_hadron', '') or '')
        be = '' if be.upper() == 'N/A' else be
        bh = '' if bh.upper() == 'N/A' else bh
        configs.append({
            'process': params.get('process', ''),
            'beam': f'{be}x{bh}' if be and bh else (be or bh),
            'species': params.get('beam_species', ''),
            'q2': params.get('q2_range', ''),
            'generator': (gen_case.get(evgen[0], evgen[0]) if evgen else ''),
            'gen_version': (evgen[1] if evgen else ''),
            'sample': detail['sample'],
            'background_tag': (head.background_tag.tag_label
                               if head.background_tag_id else ''),
            'physics': head.physics_tag.tag_label if head.physics_tag else '',
            'anchor': head.composed_name,
            'campaigns': sorted(group['campaigns']),
            'files': sum(produced.get(d.composed_name, 0)
                         for d, _ in group['editions']),
        })
    configs.sort(key=lambda c: (c['process'], c['beam'], c['q2']))
    return {'configs': configs}


def pcs_request_composer(request):
    """The request composer — a friendly front door for asking for
    production, and the first 'my epicprod' surface: the signed-in
    user's past requests and remembered defaults guide the next one.
    The mapping to PCS is deterministic (axes → the request's filter
    block; adopting an existing configuration sets the same anchor the
    CSV import writes). Read-open; submission is login-gated. The
    whole-catalog configuration list serves as a cached product
    (swf-monitor docs/CACHED_PRODUCTS.md); the user's own requests and
    preferences build per request.
    """
    from monitor_app.cached_product import get_product

    refresh = request.GET.get('refresh') == '1'
    product = get_product('pcs_composer_configs:v1',
                          _build_composer_configs,
                          ttl_seconds=300, refresh=refresh)
    configs = (product['value'] or {}).get('configs') or []

    def _options(field):
        return sorted({c[field] for c in configs if c[field]})

    username = getattr(request.user, 'username', '') or ''
    prefs = UserPreference.get_prefs(username) if username else {}
    background_tags = list(BackgroundTag.objects.order_by('tag_number'))
    background_labels = {tag.tag_label for tag in background_tags}
    backgrounds = []
    for tag in background_tags:
        params = tag.parameters or {}
        electron = str(params.get('beam_energy_electron') or '')
        hadron = str(params.get('beam_energy_hadron') or '')
        electron = '' if electron.upper() == 'N/A' else electron
        hadron = '' if hadron.upper() == 'N/A' else hadron
        backgrounds.append({
            'tag': tag.tag_label,
            'type': params.get('background_type') or '',
            'beam': (f'{electron}x{hadron}' if electron and hadron
                     else electron or hadron),
            'source': params.get('bg_source') or '',
            'mechanism': params.get('bg_mechanism') or '',
            'generator': params.get('bg_generator') or '',
            'description': tag.description or '',
        })
    my_requests = []
    if username:
        for row in (ProdRequest.objects.filter(created_by=username)
                    .order_by('-created_at')[:10]):
            row_data = row.data or {}
            filters = row_data.get('filters') or {}
            background_request = row_data.get('background_request')
            if not isinstance(background_request, dict):
                if row.background in background_labels:
                    background_request = {
                        'mode': 'registered',
                        'tag': row.background,
                    }
                elif row.background:
                    background_request = {
                        'mode': 'other',
                        'other': row.background,
                    }
                else:
                    background_request = {'mode': 'none'}
            background_mode = background_request.get('mode') or 'none'
            background_tag = background_request.get('tag') or ''
            background_other = background_request.get('other') or ''
            background_display = (
                background_tag if background_mode == 'registered'
                else background_other if background_mode == 'other'
                else 'None'
            )
            my_requests.append({
                'id': row.pk,
                'created': row.created_at.strftime('%Y-%m-%d'),
                'requestor': row.requestor,
                'status': row.status,
                'nevents': row.nevents,
                'description': row.description,
                'filters': filters,
                'anchor': row_data.get('physics_config_anchor', ''),
                'background_mode': background_mode,
                'background_tag': background_tag,
                'background_other': background_other,
                'background_display': background_display,
            })
    default_pwg = prefs.get('composer_pwg', '')
    default_dsc = prefs.get('composer_dsc', '')
    full_name = ''
    if username:
        full_name = (request.user.get_full_name() or '').strip()
    default_contact_name = prefs.get('composer_contact_name', '') or full_name
    user_email = (getattr(request.user, 'email', '') or '')
    if user_email.lower().rpartition('@')[2] in ('example.com', 'example.org'):
        # Synthetic placeholder from an old account-creation flow —
        # never offer it as real contact data.
        user_email = ''
    default_contact_email = (prefs.get('composer_contact_email', '')
                             or user_email)

    return render(request, 'pcs/request_composer.html', {
        'configs_json': json.dumps(configs),
        'process_options': _options('process'),
        'beam_options': _options('beam'),
        'species_options': _options('species'),
        'q2_options': _options('q2'),
        'generator_options': _options('generator'),
        'sample_options': _options('sample'),
        'backgrounds': backgrounds,
        'backgrounds_json': json.dumps(backgrounds),
        'pwg_options': PWG_OPTIONS,
        'dsc_option_groups': DSC_OPTION_GROUPS,
        'default_pwg': default_pwg,
        'default_dsc': default_dsc,
        'my_requests': my_requests,
        'my_requests_json': json.dumps(my_requests),
        'default_contact_name': default_contact_name,
        'default_contact_email': default_contact_email,
        'product_built_at_text': (
            product['built_at'].astimezone(_ET)
            .strftime('%Y-%m-%d %H:%M ET')
            if product['built_at'] else ''),
        'product_refreshing': product['refreshing'],
    })


def pcs_edition_data(request, name):
    """Rucio data per campaign for one physics configuration: reached
    from any of its editions, the page resolves the configuration and
    lists every physical Rucio dataset across campaigns — real DIDs
    linked to the live detail page, files, volume, per-RSE replica
    status. Read-open.

    Past/ingested rows carry their real Rucio DID in
    ``metadata.source.location`` (the ``did`` column is the PCS-internal
    name); PanDA-produced rows carry it in ``did``. Both shapes render;
    a row with no real DID shows its internal name unlinked.
    """
    from .physics_config import physics_config_key

    select = ('campaign', 'physics_tag', 'evgen_tag', 'background_tag')
    anchor = (Dataset.objects.filter(composed_name=name)
              .select_related(*select).order_by('block_num', 'pk').first())
    if anchor is None:
        raise Http404(f'No dataset identity {name!r}')
    anchor_detail = physics_config_key(anchor)
    # Sibling editions across campaigns: same-physics-tag heads (a cheap
    # prefilter) resolved to the same configuration key. An unresolved
    # anchor keys uniquely, so it matches only itself.
    edition_names = [
        head.composed_name
        for head in (Dataset.objects.filter(physics_tag=anchor.physics_tag)
                     .select_related(*select)
                     .order_by('composed_name', 'block_num', 'pk')
                     .distinct('composed_name'))
        if physics_config_key(head)['key'] == anchor_detail['key']
    ] or [name]

    rows = sorted(
        Dataset.objects.filter(composed_name__in=edition_names)
        .select_related('campaign').order_by('composed_name', 'block_num', 'pk'),
        key=lambda d: (_version_tuple(d.campaign.name if d.campaign_id else '')
                       or (0,), d.block_num, d.pk),
        reverse=True)

    params = (anchor.physics_tag.parameters or {}) if anchor.physics_tag else {}
    evgen = anchor_detail['evgen']
    gen_case = _generator_display_case()
    be = str(params.get('beam_energy_electron', '') or '')
    bh = str(params.get('beam_energy_hadron', '') or '')
    be = '' if be.upper() == 'N/A' else be
    bh = '' if bh.upper() == 'N/A' else bh
    spec_parts = [
        params.get('process', ''),
        f'{be}x{bh}' if be and bh else (be or bh),
        params.get('beam_species', ''),
        params.get('q2_range', ''),
        (' '.join(part for part in (gen_case.get(evgen[0], evgen[0]),
                                    evgen[1]) if part) if evgen else ''),
        anchor_detail['sample'],
        anchor.physics_tag.tag_label if anchor.physics_tag else '',
    ]
    pc_spec = ' · '.join(part for part in spec_parts if part)

    request_ids = set()
    for overrides in (ProdTask.objects
                      .filter(dataset__composed_name__in=edition_names)
                      .values_list('overrides', flat=True)):
        for match in (overrides or {}).get('questionnaire_matches') or []:
            qid = match.get('questionnaire_id') if isinstance(match, dict) else None
            if isinstance(qid, int) or str(qid).isdigit():
                request_ids.add(int(qid))

    # PC-anchored production requests: this page is the configuration's
    # home, so its requests render in full — a request whose anchor is
    # any of the configuration's editions belongs here.
    requests_by_anchor = services.pc_anchored_requests()
    prod_requests = sorted(
        {req.pk: req for edition in edition_names
         for req in requests_by_anchor.get(edition, ())}.values(),
        key=lambda r: r.pk)

    items = []
    total_files = 0
    total_bytes = 0
    for dataset in rows:
        metadata = dataset.metadata or {}
        location = (metadata.get('source') or {}).get('location', '')
        did = ''
        if ':' in location and '/' not in location.split(':', 1)[0]:
            did = location
        elif (dataset.did or '').startswith('group.EIC:group.EIC'):
            did = dataset.did
        scope, _, did_name = did.partition(':')
        past = metadata.get('past_output') or {}
        items.append({
            'campaign': dataset.campaign.name if dataset.campaign_id else '',
            'name': dataset.composed_name,
            'stage': (past.get('stage')
                      or str(metadata.get('stage', '')).upper()),
            'did': did,
            'did_scope': scope,
            'did_name': did_name.lstrip('/'),
            'internal': dataset.did,
            'files': dataset.file_count or 0,
            'size': dataset.data_size or 0,
            'rses': past.get('rses') or [],
            'source': location,
        })
        total_files += dataset.file_count or 0
        total_bytes += dataset.data_size or 0

    return render(request, 'pcs/edition_data.html', {
        'name': name,
        'pc_spec': pc_spec,
        'requests': sorted(request_ids),
        'prod_requests': prod_requests,
        'items': items,
        'total_files': total_files,
        'total_bytes': total_bytes,
    })


@_login_required_flash
def pcs_catalog_promote_current(request):
    """POST handler for the producing tab's 'Make <campaign> current'
    button — the lifecycle rotation, one atomic operator action: the
    named campaign becomes current, the incumbent current becomes last,
    the incumbent last becomes past. Detection is automatic (the derived
    producing status); the transition is always this human click.
    """
    from django.db import transaction

    from monitor_app.epicprod_logging import log_epicprod_action

    if request.method != 'POST':
        return _post_only_redirect(
            request, reverse('pcs:pcs_catalog'),
            action_label='Make current')
    name = (request.POST.get('name') or '').strip()
    target = Campaign.objects.filter(name=name).first()
    if target is None:
        messages.error(request, f'No campaign named {name!r}.')
        return redirect(reverse('pcs:pcs_catalog'))
    if target.lifecycle == 'current':
        messages.info(request, f'{name} is already current.')
        return redirect(reverse('pcs:pcs_catalog'))
    incumbent = (Campaign.objects.filter(lifecycle='current')
                 .exclude(pk=target.pk).first())
    if incumbent and not _promotion_forward(name, incumbent.name):
        messages.error(
            request,
            f'{name} is not newer than current campaign {incumbent.name}; '
            'rotation only moves forward.')
        return redirect(reverse('pcs:pcs_catalog'))
    moves = []
    with transaction.atomic():
        for camp in Campaign.objects.filter(lifecycle='last').exclude(pk=target.pk):
            camp.lifecycle = 'past'
            camp.save(update_fields=['lifecycle', 'updated_at'])
            moves.append(f'{camp.name} -> past')
        for camp in Campaign.objects.filter(lifecycle='current').exclude(pk=target.pk):
            camp.lifecycle = 'last'
            camp.save(update_fields=['lifecycle', 'updated_at'])
            moves.append(f'{camp.name} -> last')
        target.lifecycle = 'current'
        target.save(update_fields=['lifecycle', 'updated_at'])
        moves.append(f'{name} -> current')
    summary = '; '.join(reversed(moves))
    log_epicprod_action(
        'web', 'campaign_promoted',
        subject_type='campaign', subject_key=name,
        username=getattr(request.user, 'username', '') or '',
        sublevel='high', live_default=True,
        message=f'campaign lifecycle rotation: {summary}')
    messages.success(request, f'Campaign rotation: {summary}')
    return redirect(reverse('pcs:pcs_catalog'))


@_login_required_flash
def pcs_catalog_instancing_execute(request):
    """POST handler for the producing tab's instancing action: populate
    the target campaign's working catalog from the source campaign. The
    plan is recomputed at execution — the page's rendering is the review,
    the fresh computation is the guard."""
    from .instancing import execute_campaign_instancing

    if request.method != 'POST':
        return _post_only_redirect(
            request, reverse('pcs:pcs_catalog'),
            action_label='Campaign instancing')
    source = (request.POST.get('source') or '').strip()
    target = (request.POST.get('target') or '').strip()
    return_lifecycle = (request.POST.get('return_lifecycle') or '').strip()
    if return_lifecycle not in ('producing', 'future'):
        return_lifecycle = 'producing'
    back = (reverse('pcs:pcs_catalog')
            + f'?lifecycle={return_lifecycle}&campaign={target}')
    # The target may have no row yet only when it is the batch-derived
    # next campaign (the future tab's plan); execution creates that row.
    target_known = (
        Campaign.objects.filter(name=target).exists()
        or (_next_campaign_hint() or {}).get('name') == target)
    if not (Campaign.objects.filter(name=source).exists() and target_known):
        messages.error(request, f'Unknown campaign in {source!r} -> {target!r}.')
        return redirect(reverse('pcs:pcs_catalog'))
    if not _promotion_forward(target, source):
        messages.error(
            request,
            f'Instancing {source} -> {target} refused: the target campaign '
            'must be newer than the source.')
        return redirect(reverse('pcs:pcs_catalog'))
    result = execute_campaign_instancing(
        source, target,
        created_by=getattr(request.user, 'username', '') or '')
    s = result['summary']
    msg = (f"Instancing {source} -> {target}: {s['minted_editions']} "
           f"edition(s) minted, {s['merged_tasks']} task(s) adopted; "
           f"{s['hold']} held, {s['final']} final, {s['unresolved']} "
           f"unresolved left to curation.")
    if result['errors']:
        messages.warning(request, msg + f" {len(result['errors'])} error(s): "
                         + '; '.join(result['errors'][:3]))
    else:
        messages.success(request, msg)
    return redirect(back)


@_login_required_flash
def pcs_catalog_past_update(request):
    """POST handler for the 'Update from epic-prod' button on the Past tab.

    Runs the past-campaign output ingest (FULL + RECO 2026 versions from
    the cloned epic-prod docs tree) and redirects back to the catalog
    Past view with a flash summary. POST-only.
    """
    if request.method != 'POST':
        return _post_only_redirect(
            request,
            reverse('pcs:pcs_catalog') + '?lifecycle=past',
            action_label='Update from epic-prod')
    from .services import import_epic_prod_past_campaigns, ServiceError
    try:
        summary = import_epic_prod_past_campaigns(
            created_by=getattr(request.user, 'username', '') or 'past_import',
        )
    except (ServiceError, FileNotFoundError, OSError) as e:
        messages.error(request, f'Past-campaign import failed: {e}')
        return redirect(reverse('pcs:pcs_catalog') + '?lifecycle=past')
    msg = (f'epic-prod past import: {summary["created"]} new, '
           f'{summary["updated"]} updated, '
           f'across {summary["campaigns"]} campaigns, '
           f'{len(summary["errors"])} errors '
           f'(of {summary["rows"]} rows)')
    if summary['errors']:
        messages.warning(request, msg)
    else:
        messages.success(request, msg)
    return redirect(reverse('pcs:pcs_catalog') + '?lifecycle=past')


def assessment_register_rows(campaign_name, kinds):
    """Registered assessments for a campaign family from the local
    assessment_register action series (the same production-owned
    registration record _verdict_standing and the freshness check use)
    — no remote call in the render path. Pre-family registrations
    (subject_key 26.07.0) match through the family prefix."""
    from monitor_app.models import AppLog
    from .name_tokens import campaign_family
    family = campaign_family(campaign_name)
    names = list(dict.fromkeys([campaign_name, family]))
    name_q = Q(extra_data__subject_key__in=names)
    if family:
        name_q |= Q(extra_data__subject_key__startswith=family + '.')
    return (AppLog.objects.filter(
        name_q,
        app_name='epicprod',
        extra_data__action='assessment_register',
        extra_data__outcome='ok',
        extra_data__assessment_kind__in=kinds)
        .exclude(extra_data__contains={'quarantined': True})
        .order_by('-timestamp')
        .values('timestamp', 'extra_data'))


def assessment_human_url(campaign_name, kind, date):
    """Human-addressed assessment URL (campaign + kind + ET date or
    'latest') — the house URL form; the UUID route remains for stored
    links."""
    import logging
    try:
        return reverse('monitor_app:ai_content_by_name',
                       args=[campaign_name, kind, date])
    except Exception as e:
        logging.getLogger(__name__).warning(
            'assessment URL reverse failed for %s/%s/%s: %s',
            campaign_name, kind, date, e)
        return ''


def _latest_daily_assessment(campaign_name):
    """Latest registered daily AI assessment for a campaign, plus a link
    to the most recent weekly. Returns None when the campaign has no
    registered daily."""
    if not campaign_name:
        return None
    row = assessment_register_rows(
        campaign_name, ['daily', 'nightly']).first()
    if not row:
        return None
    extra = row['extra_data'] or {}
    report_date = timezone.localtime(row['timestamp']).date().isoformat()
    weekly = assessment_register_rows(campaign_name, ['weekly']).first()
    return {
        'campaign': campaign_name,
        'verdict': str(extra.get('verdict') or ''),
        'narration': str(extra.get('narration') or ''),
        'title': str(extra.get('report_title') or ''),
        'url': assessment_human_url(campaign_name, 'daily', report_date),
        'weekly_url': (assessment_human_url(campaign_name, 'weekly', 'latest')
                       if weekly else ''),
        'timestamp': row['timestamp'],
    }


def pcs_catalog(request):
    """Production Task Catalog — lifecycle-grouped task listing.

    Authenticated-only: the page hosts action buttons (CSV refresh,
    bulk actions, future per-task actions) whose POST handlers require
    sign-in. Catching auth at the GET prevents the silent-fail trap
    where an anonymous user sees buttons that quietly do nothing.
    """
    build_start = time.perf_counter() if CATALOG_BUILD_TIMING_ENABLED else None
    timings = [] if CATALOG_BUILD_TIMING_ENABLED else None
    filters = _timed(timings, 'parse filters', lambda: _parse_catalog_filters(request))
    inflow = _campaigns_with_inflow()
    active_lifecycle = (request.GET.get('lifecycle') or '').strip()
    producing_campaign_name = ''
    if not active_lifecycle:
        # Lifecycle-independent campaign link (?campaign=NAME): resolve
        # the tab at request time, so stored links (assessment subjects,
        # bookmarks) never go stale when a campaign's lifecycle moves.
        # A patch-level name falls back to its family, so pre-family
        # links (?campaign=26.07.0) keep resolving.
        from .name_tokens import campaign_family
        requested = (request.GET.get('campaign') or '').strip()
        for candidate in dict.fromkeys(
                [requested, campaign_family(requested)] if requested else []):
            if any(camp.name == candidate for camp, _ in inflow):
                active_lifecycle = 'producing'
                producing_campaign_name = candidate
                break
            requested_campaign = (Campaign.objects
                                  .filter(name=candidate)
                                  .only('lifecycle').first())
            if requested_campaign:
                active_lifecycle = requested_campaign.lifecycle
                break
    if active_lifecycle == 'producing':
        if not producing_campaign_name:
            producing_campaign_name = (request.GET.get('campaign') or '').strip()
        if not any(camp.name == producing_campaign_name
                   for camp, _ in inflow):
            # Not actually producing (no fresh inflow): fall back to the
            # campaign's stored lifecycle tab instead of rendering a
            # producing page with nothing behind it.
            stored = (Campaign.objects.filter(name=producing_campaign_name)
                      .only('lifecycle').first())
            active_lifecycle = (stored.lifecycle
                                if stored and stored.lifecycle in LIFECYCLE_KEYS
                                else 'current')
            producing_campaign_name = ''
    elif active_lifecycle not in LIFECYCLE_KEYS:
        active_lifecycle = 'current'
    catalog_view = (request.GET.get('view') or 'catalog').strip()
    if active_lifecycle != 'current' or catalog_view not in ('catalog', 'progress'):
        catalog_view = 'catalog'

    campaigns_by_lifecycle = _timed(
        timings,
        'campaign lifecycle query',
        lambda: {
            k: list(
                Campaign.objects
                .filter(lifecycle=k)
                .only('id', 'name', 'lifecycle', 'start_date', 'created_at')
                .order_by('name')
            )
            for k in LIFECYCLE_KEYS
        },
        detail_fn=lambda value: f'{sum(len(v) for v in value.values())} campaigns',
    )
    def _tab_detail(key, camps):
        if key == 'past':
            return ''
        return ', '.join(c.name for c in camps)
    def _tab(key, label, color):
        return {'key': key, 'label': label, 'color': color,
                'campaigns': campaigns_by_lifecycle[key],
                'detail': _tab_detail(key, campaigns_by_lifecycle[key]),
                'url': f'?lifecycle={key}',
                'active': active_lifecycle == key}
    lifecycle_tabs = [
        _tab('past', 'Past', 'secondary'),
        _tab('last', 'Last', 'last-green'),
    ]
    # Derived producing tabs: campaigns with fresh Rucio inflow, whatever
    # their stored lifecycle — current by the data's definition, so they
    # dress like Current. Placed left of Current: producing campaigns are
    # the current campaign's predecessors on the timeline.
    for camp, arrivals in inflow:
        lifecycle_tabs.append({
            'key': 'producing',
            'label': 'Producing',
            'color': 'success',
            'campaigns': [camp],
            'detail': camp.name,
            'url': f'?lifecycle=producing&campaign={camp.name}',
            'active': (active_lifecycle == 'producing'
                       and producing_campaign_name == camp.name),
        })
    lifecycle_tabs.append(_tab('current', 'Current', 'success'))
    next_hint = _next_campaign_hint()
    future_tab = _tab('future', 'Future', 'primary')
    if next_hint and not future_tab['detail']:
        future_tab['detail'] = next_hint['name']
    lifecycle_tabs.append(future_tab)

    # Past lifecycle: per-release view of output datasets. Each release
    # is one SW version (e.g. 26.04.1) covering up to two stages
    # (FULL=Simu, RECO=Reco). ?release=<v> picks one release; default is
    # the most recent. ?release=all spans every past release; release=
    # all_2025 / all_2026 spans that year. ?stage=FULL|RECO filters
    # within the chosen release set.
    #
    # Last lifecycle reuses the same path with release pinned to the
    # Last campaign's name (e.g. 26.04.1) and adds the Rucio timeline
    # plot above the table — a hybrid of Past's row model and Current's
    # snapshot view.
    if active_lifecycle in ('past', 'last', 'future', 'producing'):
        # Campaigns are bare-named (one row per version). Releases in this
        # view = versions that actually carry past-output rows, independent
        # of the lifecycle slot the campaign occupies — the current campaign
        # legitimately holds the ingested record of its pre-PanDA production.
        # The Future tab lists only future-slot campaigns' produced rows.
        rows_by_campaign = dict(
            ProdTask.objects
            .filter(status='past_output')
            .values_list('campaign__name')
            .annotate(Count('id'))
        )
        if active_lifecycle == 'future':
            future_names = {c.name for c in campaigns_by_lifecycle['future']}
            producing_names = {n for n in rows_by_campaign if n in future_names}
        else:
            producing_names = set(rows_by_campaign)
        # Time flows left to right; releases ordered ASC.
        release_versions = sorted(producing_names)
        def _version_year(v):
            head = v.split('.', 1)[0]
            return ('20' + head) if head.isdigit() and len(head) == 2 else ''
        # {'2025': [versions...], '2026': [...]} in ASC release order.
        releases_by_year = {}
        for v in release_versions:
            yr = _version_year(v)
            if yr:
                releases_by_year.setdefault(yr, []).append(v)
        # Year groups listed newest-first (2026 then 2025) per Torre's
        # preference; releases within each year stay ASC.
        years_sorted = sorted(releases_by_year.keys(), reverse=True)

        if active_lifecycle == 'last':
            # Pin release to the Last campaign's version; no nav. The
            # campaign name is the bare version ('26.04.1') and carries
            # both stages' rows.
            last_camps = campaigns_by_lifecycle['last']
            active_release = last_camps[0].name if last_camps else ''
        elif active_lifecycle == 'producing':
            # Pin release to the producing campaign the tab names.
            active_release = producing_campaign_name
        else:
            requested_release = (request.GET.get('release') or '').strip()
            if requested_release == 'all':
                active_release = 'all'
            elif (requested_release.startswith('all_')
                  and requested_release[4:] in years_sorted):
                active_release = requested_release
            elif requested_release in release_versions:
                active_release = requested_release
            else:
                # Default landing = most recent release (last in ASC).
                active_release = release_versions[-1] if release_versions else ''

        requested_stage = (request.GET.get('stage') or '').strip().upper()
        active_stage = requested_stage if requested_stage in ('FULL', 'RECO') else ''

        if active_release == 'all':
            wanted_versions = set(release_versions)
        elif active_release.startswith('all_'):
            wanted_versions = set(releases_by_year.get(active_release[4:], []))
        else:
            wanted_versions = {active_release}
        selected_names = {n for n in producing_names if n in wanted_versions}
        selected_campaigns = list(Campaign.objects.filter(name__in=selected_names))

        # Stage-facet counts from each row's produced-output stage
        # (outputs[0].stage — one entry per past-output row).
        stage_rows = (
            ProdTask.objects
            .filter(campaign__name__in=selected_names, status='past_output')
            .values_list('overrides__outputs__0__stage')
            .annotate(Count('id'))
        )
        stage_counts = {'all': 0, 'FULL': 0, 'RECO': 0}
        for stage_value, n in stage_rows:
            stage_counts['all'] += n
            if stage_value in ('FULL', 'RECO'):
                stage_counts[stage_value] += n

        past_tasks_qs = (
            ProdTask.objects
            .select_related(
                'campaign', 'dataset', 'dataset__physics_tag',
                'dataset__evgen_tag', 'dataset__simu_tag',
                'dataset__reco_tag', 'dataset__background_tag',
            )
            .filter(campaign__name__in=selected_names, status='past_output')
            .order_by('campaign__name', 'dataset__dataset_name')
        )
        if active_stage:
            past_tasks_qs = past_tasks_qs.filter(
                overrides__outputs__0__stage=active_stage)
        past_tasks = _annotate_task_questionnaire_matches(list(past_tasks_qs))

        # Aggregates from the stage-keyed past_summary, respecting the
        # stage filter.
        def _summary_total(campaign, key):
            past_summary = (campaign.data or {}).get('past_summary') or {}
            if not isinstance(past_summary, dict):
                return 0
            stages = [active_stage] if active_stage else list(past_summary)
            return sum((past_summary.get(s) or {}).get(key, 0)
                       for s in stages if isinstance(past_summary.get(s), dict))
        agg_files = sum(_summary_total(c, 'file_count') for c in selected_campaigns)
        agg_size = sum(_summary_total(c, 'data_size_bytes') for c in selected_campaigns)

        # Year groups for the template's per-year nav blocks.
        release_year_groups = [
            {'year': yr, 'versions': releases_by_year[yr],
             'all_key': f'all_{yr}'}
            for yr in years_sorted
        ]

        # Last lifecycle add-on: Rucio snapshot/timeline + Make-last
        # selector + unmatched details, layered on top of the
        # past-style table.
        rucio_timeline = None
        rucio_unmatched = []
        rucio_unmatched_campaign = ''
        rucio_detected = []
        rucio_current_name = ''
        if active_lifecycle == 'producing':
            # The producing tab gets the same arrivals timeline plot;
            # its snapshot rides the same refresh as current/last.
            from .services import load_rucio_timeline
            rucio_timeline = load_rucio_timeline(producing_campaign_name)
        if active_lifecycle == 'last':
            last_camps = campaigns_by_lifecycle['last']
            target = last_camps[0] if last_camps else None
            if target is not None:
                from .services import load_rucio_timeline
                rucio_timeline = load_rucio_timeline(target.name)
                target_data = _campaign_data(target)
                rucio_unmatched = target_data.get('rucio_unmatched', []) or []
                rucio_unmatched_campaign = target.name
                rucio_detected = target_data.get('detected_releases', []) or []
                rucio_current_name = target.name
            else:
                # No Last set yet — borrow detected releases from
                # current so the operator has options to pick from.
                cur = campaigns_by_lifecycle['current'][0] if campaigns_by_lifecycle['current'] else None
                rucio_detected = _campaign_data(cur).get('detected_releases', []) if cur else []
                rucio_current_name = cur.name if cur else ''

        producing_arrivals = None
        promote_cascade_note = ''
        promote_eligible = False
        last_is_producing = False
        producing_banner_name = producing_campaign_name
        producing_task_mix = None
        producing_table_html = None
        instancing = None
        # Unified-view convergence: last and single-release past render
        # the same curated table as Current; multi-campaign aggregates
        # ('all', year spans) keep the outputs table, their genuine role.
        tab_last_activity = ''
        if active_lifecycle == 'last' and campaigns_by_lifecycle['last']:
            tab_last_activity = _campaign_last_activity(
                campaigns_by_lifecycle['last'][0])
            producing_table_html, _, _ = _cached_current_task_list_html(
                campaigns_by_lifecycle['last'][0], 'catalog', {}, None,
                timings=timings, rebuild_on_miss=True)
        elif (active_lifecycle == 'past' and active_release
              and active_release != 'all'
              and not active_release.startswith('all_')):
            release_camp = Campaign.objects.filter(
                name=active_release).first()
            if release_camp is not None:
                producing_table_html, _, _ = _cached_current_task_list_html(
                    release_camp, 'catalog', {}, None,
                    timings=timings, rebuild_on_miss=True)
        elif active_lifecycle == 'future':
            # The next campaign gets the producing tab's instancing
            # treatment the moment it is detected: an existing
            # future-lifecycle row, or the version pending disposition
            # batches name (next_campaign_hint) before any row exists.
            candidates = [c.name for c in campaigns_by_lifecycle['future']]
            if next_hint:
                candidates.append(next_hint['name'])
            candidates = [n for n in candidates if _version_tuple(n)]
            current_names = [c.name for c in campaigns_by_lifecycle['current']]
            target_name = max(candidates, key=_version_tuple) if candidates else ''
            # Source: a producing campaign ahead of current seeds the next
            # campaign — it will be current by population time, its
            # promotion merely pending a human click; else current itself.
            source_name = current_names[0] if current_names else ''
            for camp, _arr in inflow:
                if (camp.name != target_name and _version_tuple(camp.name)
                        and (not source_name or _version_tuple(camp.name)
                             > _version_tuple(source_name))):
                    source_name = camp.name
            if target_name and source_name and target_name != source_name:
                instancing = _instancing_context(source_name, target_name)
                future_camp = Campaign.objects.filter(name=target_name).first()
                if future_camp is not None:
                    producing_table_html, _, _ = _cached_current_task_list_html(
                        future_camp, 'catalog', {}, None,
                        timings=timings, rebuild_on_miss=True)
        if active_lifecycle == 'producing':
            producing_arrivals = dict(next(
                (arr for camp, arr in inflow
                 if camp.name == producing_campaign_name), {}))
            promote_cascade_note = _promote_cascade_note(
                campaigns_by_lifecycle, producing_campaign_name)
            producing_camp = Campaign.objects.filter(
                name=producing_campaign_name).first()
            if producing_camp is not None:
                tab_last_activity = _campaign_last_activity(producing_camp)
            producing_task_mix = dict(
                ProdTask.objects.filter(campaign__name=producing_campaign_name)
                .values_list('status').annotate(Count('id')))
            # The unified curated view (CAMPAIGN_CONTINUUM.md): the
            # producing campaign renders the same task table as Current.
            if producing_camp is not None:
                producing_table_html, _, _ = _cached_current_task_list_html(
                    producing_camp, 'catalog', {}, None, timings=timings,
                    rebuild_on_miss=True)
            # Populate and promote exist for the pre-promotion state only:
            # a NEWER campaign producing ahead of its promotion click
            # (_promotion_forward).
            current_names = [c.name for c in campaigns_by_lifecycle['current']]
            promote_eligible = _promotion_forward(
                producing_campaign_name,
                current_names[0] if current_names else '')
            if (current_names and producing_campaign_name not in current_names
                    and promote_eligible):
                instancing = _instancing_context(current_names[0],
                                                 producing_campaign_name)
        if active_lifecycle == 'last' and campaigns_by_lifecycle['last']:
            last_name = campaigns_by_lifecycle['last'][0].name
            last_arr = next((arr for camp, arr in inflow
                             if camp.name == last_name), None)
            if last_arr is not None:
                # The Last campaign is still producing (fresh inflow):
                # same active treatment as the Producing tab — arrivals
                # banner, task mix, and the assessment strip.
                last_is_producing = True
                producing_banner_name = last_name
                producing_arrivals = dict(last_arr)
                producing_task_mix = dict(
                    ProdTask.objects.filter(campaign__name=last_name)
                    .values_list('status').annotate(Count('id')))

        return render(request, 'pcs/pcs_catalog_past.html', {
            'show_tabs': True,
            'campaign_assessment': (
                _latest_daily_assessment(producing_banner_name)
                if (active_lifecycle == 'producing' or last_is_producing)
                else None),
            'last_is_producing': last_is_producing,
            'producing_banner_name': producing_banner_name,
            'next_campaign_hint': (next_hint
                                   if active_lifecycle == 'future' else None),
            'producing_campaign': producing_campaign_name,
            'producing_arrivals': producing_arrivals,
            'promote_eligible': promote_eligible,
            'tab_last_activity': tab_last_activity,
            'producing_task_mix': producing_task_mix,
            'task_list_html': producing_table_html,
            'promote_cascade_note': promote_cascade_note,
            'instancing': instancing,
            'active_lifecycle': active_lifecycle,
            'lifecycle_tabs': lifecycle_tabs,
            'release_versions': release_versions,
            'release_year_groups': release_year_groups,
            'active_release': active_release,
            'active_stage': active_stage,
            'stage_counts': stage_counts,
            'selected_campaign_count': len(selected_campaigns),
            'aggregate_file_count': agg_files,
            'aggregate_data_size': agg_size,
            'tasks': past_tasks,
            'rucio_timeline_json': json.dumps(rucio_timeline) if rucio_timeline else 'null',
            'rucio_unmatched': rucio_unmatched,
            'rucio_unmatched_campaign': rucio_unmatched_campaign,
            'rucio_detected': rucio_detected,
            'rucio_current_name': rucio_current_name,
        })

    # Rucio arrivals timeline for the current campaign (when a snapshot
    # exists). Surfaced at the top of the page as a Plotly chart.
    rucio_timeline = None
    rucio_unmatched = []
    rucio_unmatched_campaign = ''
    rucio_detected = []
    rucio_current_name = ''
    evgen_rucio_unmatched = []
    evgen_rucio_checked_at = ''
    if active_lifecycle == 'current':
        camp_list = campaigns_by_lifecycle['current']
        target = camp_list[0] if camp_list else None
        if target is not None:
            from .services import load_rucio_timeline
            rucio_timeline = _timed(
                timings,
                'Rucio timeline cached read',
                lambda: load_rucio_timeline(target.name),
                detail_fn=lambda value: (
                    f'{len(value.get("dates") or [])} bins'
                    if value else 'missing'
                ),
            )
            data_start = time.perf_counter()
            target_data = _campaign_data(target)
            rucio_unmatched = target_data.get('rucio_unmatched', []) or []
            rucio_unmatched_campaign = target.name
            # The current tab offers only genuinely NEW detected releases
            # (a one-click switch forward): newer than current AND not
            # already occupying a catalog lifecycle slot — a retired
            # interim (26.06.0, past) is never re-promoted, and older
            # releases' content lives under Past.
            current_version = _version_tuple(target.name)
            known_names = set(Campaign.objects.exclude(lifecycle='future')
                              .values_list('name', flat=True))
            rucio_detected = [
                r for r in (target_data.get('detected_releases', []) or [])
                if current_version and _version_tuple(r.get('version'))
                and _version_tuple(r.get('version')) > current_version
                and r.get('version') not in known_names
            ]
            rucio_current_name = target.name
            evgen_rucio_unmatched = target_data.get('evgen_rucio_unmatched', []) or []
            evgen_rucio_checked_at = target_data.get('evgen_rucio_checked_at', '')
            _timing_record(
                timings,
                'Rucio cached metadata read',
                data_start,
                detail=f'{len(rucio_unmatched)} unmatched, {len(evgen_rucio_unmatched)} EVGEN unmatched',
            )

    progress_snapshot = None
    progress_refresh_requested = request.GET.get('refresh') == '1'
    progress_refreshed_for_request = False
    progress_refresh_error = ''
    progress_campaign = campaigns_by_lifecycle['current'][0] if campaigns_by_lifecycle['current'] else None
    if progress_campaign is not None:
        from .services import load_campaign_progress_snapshot
        progress_snapshot = _timed(
            timings,
            'progress snapshot cached read',
            lambda: load_campaign_progress_snapshot(progress_campaign),
            detail_fn=lambda value: (
                'generated_at=' + str((value or {}).get('generated_at') or '')
                if value else 'missing'
            ),
        )
    rucio_json = _timed(
        timings,
        'Rucio chart JSON encode',
        lambda: json.dumps(rucio_timeline) if rucio_timeline else 'null',
        detail_fn=lambda value: f'{len(value)} bytes',
    )
    requestor_options = _timed(
        timings,
        'requestor filter options',
        _requestor_options,
        detail_fn=lambda value: f'{len(value)} options',
    )
    propagation_last_comment = ''
    if request.user.is_authenticated:
        from monitor_app.models import UserPreference
        propagation_last_comment = UserPreference.get_prefs(
            request.user.username).get('propagation_last_comment', '')
    current_camp = (campaigns_by_lifecycle['current'][0]
                    if campaigns_by_lifecycle['current'] else None)
    context = {
        'propagation_last_comment': propagation_last_comment,
        'tasks': [],
        'ai_executed_names': _executed_proposal_names(),
        'current_last_activity': (_campaign_last_activity(current_camp)
                                  if current_camp else ''),
        'campaign_assessment': (_latest_daily_assessment(current_camp.name)
                                if current_camp else None),
        'promote_offers': [
            {'name': camp.name,
             'note': _promote_cascade_note(campaigns_by_lifecycle, camp.name)}
            for camp, _ in inflow
            if _promotion_forward(
                camp.name, current_camp.name if current_camp else '')
        ],
        'show_tabs': True,
        'columns_mode': 'full',
        'catalog_view': catalog_view,
        'catalog_view_urls': {
            'catalog': _catalog_view_url(request, active_lifecycle, 'catalog'),
            'progress': _catalog_view_url(request, active_lifecycle, 'progress'),
        },
        'active_lifecycle': active_lifecycle,
        'lifecycle_tabs': lifecycle_tabs,
        'active_campaigns': campaigns_by_lifecycle[active_lifecycle],
        'progress_campaign_name': progress_campaign.name if progress_campaign else '',
        'focused_campaign': None,
        'focused_task_id': None,
        'filters': filters,
        'requestor_options': requestor_options,
        'status_choices': PRODTASK_STATUS_CHOICES,
        'form_action': reverse('pcs:pcs_catalog'),
        'rucio_timeline_json': rucio_json,
        'rucio_unmatched': rucio_unmatched,
        'rucio_unmatched_campaign': rucio_unmatched_campaign,
        'rucio_detected': rucio_detected,
        'rucio_current_name': rucio_current_name,
        'evgen_rucio_unmatched': evgen_rucio_unmatched,
        'evgen_rucio_checked_at': evgen_rucio_checked_at,
        'progress_snapshot': progress_snapshot,
        'progress_errors': (progress_snapshot or {}).get('errors') or [],
        'progress_generated_at': (progress_snapshot or {}).get('generated_at') or '',
        'progress_generated_by': (progress_snapshot or {}).get('generated_by') or '',
        'progress_refresh_requested': progress_refresh_requested,
        'progress_refreshed_for_request': progress_refreshed_for_request,
        'progress_refresh_error': progress_refresh_error,
    }
    task_list_html, task_list_cache_hit, task_list_cache_meta = _cached_current_task_list_html(
        progress_campaign, catalog_view, context, progress_snapshot, timings=timings)
    context['task_list_html'] = task_list_html
    context['task_list_cache_hit'] = task_list_cache_hit
    context['task_list_cache_rendered_at'] = task_list_cache_meta.get('rendered_at') or ''
    context['task_list_cache_stale'] = bool(task_list_cache_meta.get('stale'))
    context['task_list_cache_miss_suppressed'] = bool(
        task_list_cache_meta.get('cache_miss_suppressed'))
    if CATALOG_BUILD_TIMING_ENABLED:
        context['catalog_timing_rows'] = timings
        context['catalog_timing_total_ms'] = _timing_ms(time.perf_counter() - build_start)
    return render(request, 'pcs/pcs_catalog.html', context)


def prod_tasks_list(request):
    columns = [
        {'name': 'name', 'title': 'Name', 'orderable': True},
        {'name': 'status', 'title': 'Status', 'orderable': True},
        {'name': 'dataset__dataset_name', 'title': 'Dataset', 'orderable': True},
        {'name': 'prod_config__name', 'title': 'Config', 'orderable': True},
        {'name': 'created_by', 'title': 'Created By', 'orderable': True},
        {'name': 'updated_at', 'title': 'Updated', 'orderable': True},
    ]
    context = {
        'table_title': 'Production Tasks',
        'table_description': 'Production task compositions (Dataset + Config).',
        'ajax_url': reverse('pcs:prod_tasks_datatable_ajax'),
        'columns': columns,
    }
    return render(request, 'pcs/prod_tasks_list.html', context)


def prod_tasks_datatable_ajax(request):
    col_names = ['name', 'status', 'dataset__dataset_name', 'prod_config__name',
                 'created_by', 'updated_at']
    dt = DataTablesProcessor(request, col_names, default_order_column=5, default_order_direction='desc')

    qs = ProdTask.objects.select_related('dataset', 'prod_config')
    records_total = qs.count()
    search_fields = ['name', 'description', 'dataset__composed_name', 'dataset__dataset_name', 'prod_config__name', 'created_by']
    qs = dt.apply_search(qs, search_fields)
    records_filtered = qs.count()
    qs = qs.order_by(dt.get_order_by())
    page = dt.apply_pagination(qs)

    status_colors = {'draft': 'secondary', 'ready': 'primary', 'submitted': 'info',
                     'completed': 'success', 'failed': 'danger'}
    data = []
    for t in page:
        detail_url = reverse('pcs:prod_task_detail', args=[t.composed_name])
        color = status_colors.get(t.status, 'secondary')
        data.append([
            f'<a href="{detail_url}">{t.composed_name}</a>',
            f'<span class="badge bg-{color}">{t.status}</span>',
            t.dataset.dataset_name,
            t.prod_config.name,
            t.created_by,
            format_datetime(t.updated_at),
        ])

    return dt.create_response(data, records_total, records_filtered)


def prod_task_detail(request, name):
    from .commands import build_evgen_task_params
    from .services import resolve_prodtask
    try:
        task = resolve_prodtask(name, ProdTask.objects.select_related(
            'dataset', 'dataset__physics_tag', 'dataset__evgen_tag',
            'dataset__simu_tag', 'dataset__reco_tag', 'prod_config',
        ).prefetch_related('panda_tasks'))
    except ProdTask.DoesNotExist:
        raise Http404(f"No task {name!r}")
    # Canonical task URL is the composed name; 301 a legacy/raw-name or stale
    # /tasks/<pk>/ inbound to it so a pk is never a resting URL.
    if name != task.composed_name:
        return redirect('pcs:prod_task_detail', name=task.composed_name, permanent=True)
    try:
        task_params = build_evgen_task_params(task)
        task_params_json = json.dumps(task_params, indent=2, sort_keys=False, default=str)
        task_params_error = None
    except Exception as e:
        task_params_json = None
        task_params_error = str(e)
    can_operate = request.user.is_authenticated
    return render(request, 'pcs/prod_task_detail.html', {
        'task': task,
        'task_params_json': task_params_json,
        'task_params_error': task_params_error,
        'can_operate': can_operate,
        'panda_tasks': services.panda_tasks_summary(task, include_live=True),
        'can_submit': can_operate and task.panda_task_id is None and task.status in ('draft', 'ready'),
        'can_reset_submission': False,
    })


def prod_task_compose(request):
    """Two-pane compose UI for building production tasks.

    The page is scoped to ONE campaign — the current campaign by default, or the
    campaign of the ?selected=<name> task. Only that campaign's tasks, and the
    datasets they use, are shipped inline; cross-campaign and historical browsing
    is the full catalog's job (linked from the page caption). Per-item heavy
    detail (tag parameters, EVGEN submission spec, cached commands) is still omitted and
    hydrated on open (prod_task_compose_dataset_detail / _task_detail).
    """
    # Resolve the campaign first — it scopes the whole page. Default = the
    # current campaign; a ?selected=<name> task in another campaign follows that
    # task's campaign.
    selected_name = request.GET.get('selected') or None
    focused_task = None
    ambiguous_matches = None
    if selected_name:
        from .services import AmbiguousIdentity, resolve_prodtask
        try:
            focused_task = resolve_prodtask(
                selected_name, ProdTask.objects.select_related('campaign', 'dataset'))
        except AmbiguousIdentity as exc:
            ambiguous_matches = [
                {
                    'composed_name': t.composed_name,
                    'name': t.name,
                    'status': t.status,
                    'jedi_task_id': t.panda_task_id,
                }
                for t in exc.matches
            ]
        except ProdTask.DoesNotExist:
            focused_task = None
    # Hand the JS the canonical composed name as the selection key (it resolves
    # composed-name-or-legacy), so a legacy-name or pk ?selected still focuses.
    if focused_task is not None:
        selected_name = focused_task.composed_name
    campaign = focused_task.campaign if (focused_task and focused_task.campaign) else None
    if campaign is None:
        campaign = Campaign.objects.filter(lifecycle='current').order_by('name').first()

    # Campaign-scoped task set — the single inline JSON source. Shipping every
    # campaign's tasks (and the ~4900 past_output archive rows) was what made
    # this page multi-MB and prone to proxy read timeouts.
    tasks_list = []
    if campaign is not None:
        tasks_list = list(
            ProdTask.objects.select_related(
                'dataset', 'dataset__physics_tag', 'dataset__evgen_tag',
                'dataset__simu_tag', 'dataset__reco_tag',
                'dataset__background_tag', 'prod_config',
            ).prefetch_related('panda_tasks').filter(campaign=campaign).order_by('-updated_at')
        )
    # Light task entries: EVGEN submission spec + cached commands omitted, hydrated on
    # open (prod_task_compose_task_detail). Readiness (cheap) is included so the
    # detail panel can show submit-readiness without a round trip.
    from .services import prodtask_readiness_problems
    tasks_list = _annotate_task_questionnaire_matches(tasks_list)
    tasks_list = _annotate_task_pc_requests(tasks_list)
    tasks_list = annotate_pwg_priority(tasks_list)
    pwg_marks = {m.path: int(m.priority)
                 for m in EvgenMark.objects.filter(priority__gt=0)}
    tasks_data = []
    for t in tasks_list:
        tasks_data.append({
            'id': t.id,
            'name': t.name,
            # Canonical identity (stored dataset.composed_name); the JS keys and
            # links tasks on this, never on the pk or the legacy slash name.
            'composed_name': t.composed_name,
            'status': t.status,
            # PWG priority of the EVGEN input (1 = highest, 0 = unset), and
            # the per-path levels the detail's button row sets.
            'pwg_priority': t.pwg_priority,
            'evgen_priority': [
                {'path': p, 'priority': pwg_marks.get(p, 0)}
                for p in t.evgen_paths],
            # The recorded submission — the JS reads `submitted = !!t.panda_task_id`
            # to show the PanDA-task link + the operator Reset control. Omitting it
            # left every submitted task with only the Copy button on page load.
            'panda_task_id': t.panda_task_id,
            'panda_tasks': services.panda_tasks_summary(t),
            'dataset_id': t.dataset_id,
            'dataset_name': t.dataset.dataset_name,
            'prod_config_id': t.prod_config_id,
            'prod_config_name': t.prod_config.name,
            'csv_file': t.csv_file,
            'overrides': t.overrides or {},
            'ai_content': ai_content_summary(t.overrides or {}),
            'propagation': t.dataset.propagation if t.dataset_id else '',
            'proposal': ((t.dataset.metadata or {}).get('proposal')
                         if t.dataset_id else None),
            'description': t.description,
            'created_by': t.created_by,
            'readiness': prodtask_readiness_problems(t),
            'updated_at': format_datetime(t.updated_at),
            'questionnaire_matches': [
                {
                    'id': item['questionnaire'].pk,
                    'confidence': item.get('confidence') or '',
                    'reason': item.get('reason') or '',
                }
                for item in getattr(t, 'questionnaire_matches', [])
            ],
            # PC-projected production requests (CAMPAIGN_CONTINUUM.md):
            # the task reaches them through its physics configuration.
            'pc_requests': [
                {
                    'id': req.pk,
                    'requestor': req.requestor,
                    'nevents': req.nevents,
                    'description': req.description,
                    'issue_url': req.source_url,
                }
                for req in getattr(t, 'pc_requests', [])
            ],
        })

    # Datasets: only those used by the in-scope tasks — campaign-coherent, and
    # keeps the past_output archive datasets off the page.
    dataset_ids = {t.dataset_id for t in tasks_list}
    datasets_qs = Dataset.objects.filter(id__in=dataset_ids).select_related(
        'physics_tag', 'evgen_tag', 'simu_tag', 'reco_tag', 'background_tag',
    ).order_by('-created_at')
    datasets_data = []
    for ds in datasets_qs:
        datasets_data.append({
            'id': ds.id,
            'dataset_name': ds.dataset_name,
            'did': ds.did,
            'scope': ds.scope,
            'detector_version': ds.detector_version,
            'detector_config': ds.detector_config,
            'stage': ds.stage,
            'external': ds.is_external,
            'source_kind': ds.source_kind,
            'source_location': ds.source_location,
            'validation_status': ds.validation_status,
            # tag .parameters and .metadata omitted from the light payload;
            # hydrated on open (prod_task_compose_dataset_detail). Labels +
            # descriptions stay for the list, search, and the diff.
            'physics_tag': {'label': ds.physics_tag.tag_label, 'description': ds.physics_tag.description},
            'evgen_tag': {'label': ds.evgen_tag.tag_label, 'description': ds.evgen_tag.description},
            'simu_tag': {'label': ds.simu_tag.tag_label, 'description': ds.simu_tag.description},
            'reco_tag': {'label': ds.reco_tag.tag_label, 'description': ds.reco_tag.description},
            # Background (k) is optional; null when the dataset carries no
            # standalone-background tag.
            'background_tag': ({'label': ds.background_tag.tag_label,
                                'description': ds.background_tag.description}
                               if ds.background_tag_id else None),
            # The dataset's name in the tag-based system. The human-facing
            # identity on the page — the internal csv_import.<hash> dataset_name
            # and its synthetic DID are plumbing and are not shown.
            'composed_name': ds.build_dataset_name(),
            'created_by': ds.created_by,
            'created_at': ds.created_at.strftime('%Y-%m-%d %H:%M'),
        })

    configs_qs = ProdConfig.objects.order_by('-updated_at')
    configs_data = []
    for pc in configs_qs:
        configs_data.append({
            'id': pc.id,
            'name': pc.name,
            'description': pc.description,
            'jug_xl_tag': pc.jug_xl_tag,
            'container_image': pc.container_image,
            'bg_mixing': pc.bg_mixing,
            'bg_cross_section': pc.bg_cross_section,
            'bg_evtgen_file': pc.bg_evtgen_file,
            'copy_reco': pc.copy_reco,
            'copy_full': pc.copy_full,
            'copy_log': pc.copy_log,
            'use_rucio': pc.use_rucio,
            'target_hours_per_job': str(pc.target_hours_per_job) if pc.target_hours_per_job else '',
            'events_per_task': pc.events_per_task,
            'panda_site': pc.panda_site,
            'panda_queue': pc.panda_queue,
            'panda_working_group': pc.panda_working_group,
            'panda_resource_type': pc.panda_resource_type,
            'rucio_rse': pc.rucio_rse,
            'data': pc.data or {},
            'created_by': pc.created_by,
            'updated_at': pc.updated_at.strftime('%Y-%m-%d %H:%M'),
        })

    # Left-panel task list: same campaign scope, dataset-name order, the
    # partial-friendly shape the catalog uses.
    campaign_tasks = []
    if campaign is not None:
        campaign_tasks = list(
            ProdTask.objects
            .select_related(
                'campaign', 'dataset', 'prod_config', 'request',
                # The compose list falls back to dataset.composed_name (5 tag
                # FKs) when a row has no source path; prefetch so native-dataset
                # campaigns don't hit the same 1 + 5N as the catalog (a9a93ae).
                'dataset__physics_tag', 'dataset__evgen_tag',
                'dataset__simu_tag', 'dataset__reco_tag',
                'dataset__background_tag',
            )
            .filter(campaign=campaign)
            .order_by('dataset__dataset_name')
        )
        campaign_tasks = _annotate_task_questionnaire_matches(campaign_tasks)

    context = {
        'datasets_json': json.dumps(datasets_data),
        'configs_json': json.dumps(configs_data),
        'tasks_json': json.dumps(tasks_data),
        'selected_item_json': json.dumps(selected_name),
        'username': request.user.username if request.user.is_authenticated else '',
        # Left-panel task-list context (consumed by the list partial):
        'tasks': campaign_tasks,
        'ai_executed_names': _executed_proposal_names(),
        'focused_task_id': focused_task.id if focused_task else None,
        'focused_campaign': campaign,
        'ambiguous_selected': request.GET.get('selected') if ambiguous_matches else '',
        'ambiguous_matches': ambiguous_matches or [],
        'filters': {},
    }
    return render(request, 'pcs/prod_task_compose.html', context)


@_login_required_flash
def prod_task_delete(request, name):
    from .services import resolve_prodtask
    try:
        task = resolve_prodtask(name, ProdTask.objects.select_related('dataset'))
    except ProdTask.DoesNotExist:
        raise Http404(f"No task {name!r}")
    if request.method != 'POST':
        return _post_only_redirect(
            request, reverse('pcs:prod_task_detail', kwargs={'name': task.composed_name}),
            action_label='Task delete')
    if task.status != 'draft':
        messages.error(request, "Only draft tasks can be deleted.")
        return redirect('pcs:prod_task_detail', name=task.composed_name)
    task.delete()
    messages.success(request, f"Task '{task.composed_name}' deleted.")
    log_epicprod_action(
        'web', 'task_delete', subject_type='campaign_task',
        subject_key=task.composed_name,
        username=getattr(request.user, 'username', ''),
        sublevel='normal', live_default=True)
    return redirect('pcs:prod_tasks_list')


def prod_task_generate_commands(request, name):
    """JSON endpoint: regenerate and return commands for a ProdTask."""
    from .services import resolve_prodtask
    try:
        task = resolve_prodtask(name, ProdTask.objects.select_related(
            'dataset', 'dataset__physics_tag', 'dataset__evgen_tag',
            'dataset__simu_tag', 'dataset__reco_tag', 'prod_config',
        ))
    except ProdTask.DoesNotExist:
        raise Http404(f"No task {name!r}")
    task.generate_commands()
    task.save(update_fields=['condor_command', 'panda_command', 'updated_at'])
    return JsonResponse({
        'condor_command': task.condor_command,
        'panda_command': task.panda_command,
    })


def prod_task_compose_dataset_detail(request, pk):
    """On-demand hydration for the compose view: a dataset's tag parameters and
    metadata, which the light initial payload omits. The compose JS merges this
    into the dataset entry the first time it is opened (never clobbering). GET
    JSON; read-only."""
    ds = get_object_or_404(Dataset.objects.select_related(
        'physics_tag', 'evgen_tag', 'simu_tag', 'reco_tag', 'background_tag'), pk=pk)
    payload = {
        'physics_tag': {'parameters': ds.physics_tag.parameters},
        'evgen_tag': {'parameters': ds.evgen_tag.parameters},
        'simu_tag': {'parameters': ds.simu_tag.parameters},
        'reco_tag': {'parameters': ds.reco_tag.parameters},
        'metadata': ds.metadata or {},
    }
    if ds.background_tag_id:
        payload['background_tag'] = {'parameters': ds.background_tag.parameters}
    return JsonResponse(payload)


def prod_task_compose_task_detail(request, name):
    """On-demand hydration for the compose view: a task's live EVGEN submission
    spec and cached condor/panda commands, which the light initial payload omits.
    The compose JS merges this into the task entry the first time it is opened
    (never clobbering). GET JSON; read-only — does not regenerate/save commands."""
    from .commands import build_evgen_task_params
    from .services import resolve_prodtask
    try:
        task = resolve_prodtask(name, ProdTask.objects.select_related(
            'dataset', 'dataset__physics_tag', 'dataset__evgen_tag',
            'dataset__simu_tag', 'dataset__reco_tag', 'prod_config',
        ).prefetch_related('panda_tasks'))
    except ProdTask.DoesNotExist:
        raise Http404(f"No task {name!r}")
    try:
        task_params_json = json.dumps(build_evgen_task_params(task), indent=2, default=str)
        task_params_error = ''
    except Exception as e:                                       # noqa: BLE001
        task_params_json = ''
        task_params_error = str(e)
    return JsonResponse({
        'task_params_json': task_params_json,
        'task_params_error': task_params_error,
        'condor_command': task.condor_command,
        'panda_command': task.panda_command,
        'panda_tasks': services.panda_tasks_summary(task, include_live=True),
        'ai_content': ai_content_summary(task.overrides or {}),
    })
