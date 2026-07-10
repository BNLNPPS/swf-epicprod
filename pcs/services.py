"""
PCS business logic — single source of truth for intake, linkage,
lifecycle, and submission state changes.

Both REST viewset actions and MCP tools call these functions. Functions
take plain Python types (no HTTP request, no MCP context) and return
model instances or raise ServiceError. The caller translates errors
to its native shape (DRF Response, MCP error dict).
"""
import csv as _csv
import hashlib as _hashlib
import io as _io
import json as _json
import logging as _logging
import os as _os
import re as _re
import uuid as _uuid
from datetime import datetime as _datetime

from django.conf import settings as _settings
from django.core.cache import cache as _cache
from django.db import connections, transaction
from django.db.models import Q
from django.utils import timezone as _timezone
from django.utils.dateparse import parse_datetime as _parse_datetime

from monitor_app.utils import format_datetime

_log = _logging.getLogger(__name__)

from .models import (
    Dataset, ProdConfig, ProdTask, PandaTasks,
    Campaign, Questionnaire, ProdRequest,
    PhysicsCategory, PhysicsTag, EvgenTag, SimuTag, RecoTag, BackgroundTag,
)
from .name_tokens import (
    logical_name_from_physical_name,
    panda_attempt_name,
    panda_name_from_physical_name,
    try_number_from_physical_name,
)
from .physics_match import derive_physics, derive_background, derive_evgen, single_particle_angle


class ServiceError(Exception):
    """Domain error with an HTTP-shaped status hint and detail message."""
    def __init__(self, detail, status=400):
        self.detail = detail
        self.status = status
        super().__init__(detail)


PROGRESS_SNAPSHOT_KEY = 'progress_snapshot'


def campaign_progress_snapshot_cache_key(campaign):
    if campaign is None:
        return ''
    return f'pcs:campaign-progress-snapshot:{campaign.pk}:{campaign.name}'


def load_campaign_progress_snapshot(campaign):
    key = campaign_progress_snapshot_cache_key(campaign)
    return _cache.get(key) if key else None


# Allowed ProdTask lifecycle transitions. Submission and post-submission
# state changes are recorded via prodtask_record_submission and
# automation, not direct human transitions.
#
# Imported catalog rows enter as ``draft`` and follow the normal flow. The one
# import status that is NOT a key here is ``past_output``: a frozen historical
# archive (terminal — clone it, via Copy, to base new production on it), so the
# generic set-status path can never move it. See
# PCS_DATASET_REQUEST_WORKFLOW.md §Lifecycle and EPICPROD_TASK_CATALOG.md §6.
PRODTASK_TRANSITIONS = {
    'draft':     {'ready'},
    'ready':     {'draft', 'submitted'},
    'submitted': {'completed', 'failed'},
    'completed': set(),
    'failed':    set(),
}

PUBLIC_CATALOG_KEYS = (
    'public_catalog_repo', 'public_catalog_issue', 'public_catalog_pr',
    'public_catalog_row_index', 'public_catalog_csv_path',
    'public_catalog_row_key', 'public_catalog_page_url',
    'public_catalog_commit_sha',
)

# Fields copied from ProdRequest → ProdTask at task creation. Both rows
# are independently mutable thereafter. Per
# memory:feedback-denormalization-ok — the duplication is intentional so
# catalog filter/display reads direct ProdTask columns without joining.
REQUEST_TO_TASK_COPY_FIELDS = (
    'requestor', 'priority',
    'pre_tdr_use', 'early_science_use', 'other_use', 'new_request',
)


def _to_int(value, default=0):
    try:
        if value in (None, ''):
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _progress_output_file_count(output):
    """Best available dataset-level file count from a recorded output entry."""
    if not isinstance(output, dict):
        return 0
    count = _to_int(output.get('file_count'), 0)
    for rse in output.get('rses') or []:
        count = max(count, _to_int(rse.get('files'), 0))
    return count


def _progress_candidate_tasknames(task, output):
    names = []
    for value in (task.composed_name, task.name):
        if value:
            names.append(value)
    did = (output or {}).get('did') if isinstance(output, dict) else ''
    if did and ':' in did:
        did_name = did.split(':', 1)[1]
        names.extend([did_name, did_name.lstrip('/')])
        parts = [p for p in did_name.strip('/').split('/') if p]
        if len(parts) >= 3 and parts[0] in ('FULL', 'RECO', 'SIMU'):
            # Current legacy production names JLab Rucio outputs as
            # epic:/RECO/<campaign>/<detector>/<path...>, while PanDA task
            # names use group.EIC.<campaign>.<detector>.<path...>.
            names.append('group.EIC.' + '.'.join(parts[1:]))
        if did_name.endswith('.b1'):
            names.extend([did_name[:-3], did_name.lstrip('/')[:-3]])
    elif did:
        names.append(did)
    more = []
    replacements = (
        ('.e+.', '.e_plus.'),
        ('.e-.', '.e_minus.'),
        ('.pi+.', '.pi_plus.'),
        ('.pi-.', '.pi_minus.'),
    )
    for name in names:
        variants = {name}
        for a, b in replacements:
            new_variants = set()
            for v in variants:
                new_variants.add(v.replace(a, b))
                new_variants.add(v.replace(b, a))
            variants |= new_variants
        more.extend(variants)
    names.extend(more)
    return [n for i, n in enumerate(names) if n and n not in names[:i]]


def panda_tasks_physical_name(task, try_number):
    """Physical PanDA task/output name for one submission attempt."""
    return panda_attempt_name(task.composed_name, try_number)


def _log_dataset_name(task_name):
    return f'{task_name}_log/'


def panda_tasks_summary(task, *, include_live=False):
    """Compact association history for API/UI payloads.

    The compose page ships many task summaries, so live PanDA enrichment is
    opt-in. Use include_live=True only for focused task hydration or small
    detail payloads.
    """
    rows = getattr(task, '_prefetched_objects_cache', {}).get('panda_tasks')
    if rows is None:
        rows = task.panda_tasks.all()
    rows = list(rows)
    live_by_id = {}
    live_by_name = {}
    if include_live:
        ids = [row.jedi_task_id for row in rows if row.jedi_task_id]
        names = [row.task_name for row in rows if row.task_name]
        live_by_id, live_by_name, _ = _panda_progress_summaries(ids, names)

    summaries = []
    for row in sorted(rows, key=lambda item: item.try_number):
        live = (
            live_by_id.get(_to_int(row.jedi_task_id))
            or live_by_name.get(row.task_name)
            or {}
        )
        metadata = row.metadata or {}
        maxattempt = live.get('maxattempt')
        if maxattempt is None:
            maxattempt = metadata.get('maxattempt') or metadata.get('panda_maxattempt')
        if include_live and live:
            updates = {
                key: live.get(key)
                for key in ('status', 'maxattempt', 'nactive', 'nfinished',
                            'nfailed', 'nfinalfailed', 'nrunning', 'total_jobs',
                            'terminal_jobs', 'processing_percent',
                            'final_failure_rate')
                if live.get(key) is not None
            }
            if updates:
                metadata = {**metadata, **updates}
        summaries.append({
            'id': row.pk,
            'try_number': row.try_number,
            'jedi_task_id': row.jedi_task_id,
            'task_name': row.task_name,
            'out_ds': row.out_ds,
            'log_ds': row.log_ds,
            'site': live.get('site') or row.site,
            'status_snapshot': live.get('status') or row.status_snapshot,
            'association_source': row.association_source,
            'match_reason': row.match_reason,
            'maxattempt': maxattempt,
            'metadata': metadata,
            'current': bool(task.panda_task_id and row.jedi_task_id == task.panda_task_id),
            'created_at': row.created_at.isoformat() if row.created_at else '',
            'created_at_display': format_datetime(row.created_at) if row.created_at else '',
            'updated_at': row.updated_at.isoformat() if row.updated_at else '',
            'updated_at_display': format_datetime(row.updated_at) if row.updated_at else '',
        })
    return summaries


def prodtask_allocate_panda_tasks(*, task, source='pcs_submit_request'):
    """Create the next PandaTasks association row before submission."""
    with transaction.atomic():
        locked = ProdTask.objects.select_for_update().get(pk=task.pk)
        last_try = (
            PandaTasks.objects
            .filter(prod_task=locked)
            .order_by('-try_number')
            .values_list('try_number', flat=True)
            .first()
        )
        try_number = (last_try or 0) + 1
        task_name = panda_tasks_physical_name(locked, try_number)
        row = PandaTasks.objects.create(
            prod_task=locked,
            try_number=try_number,
            task_name=task_name,
            out_ds=task_name,
            log_ds=_log_dataset_name(task_name),
            association_source=source,
            match_reason='allocated before PCS submission',
        )
    return row


def _try_number_from_physical_name(logical_name, physical_name):
    return try_number_from_physical_name(logical_name, physical_name)


def _next_try_number(task):
    last_try = (
        PandaTasks.objects
        .filter(prod_task=task)
        .order_by('-try_number')
        .values_list('try_number', flat=True)
        .first()
    )
    return (last_try or 0) + 1


def _match_prod_tasks_for_panda_name(panda_task_name):
    """Return exact PCS task matches for a PanDA taskName.

    No fuzzy matching: the physical name must reduce via registered suffix
    parsing to the PCS composed identity, the legacy stored task name, or one
    of the exact candidate names already used by campaign progress.
    """
    name = (panda_task_name or '').strip()
    if not name:
        return []
    base = logical_name_from_physical_name(name)
    direct = list(
        ProdTask.objects
        .select_related('dataset', 'prod_config')
        .filter(Q(dataset__composed_name=base) | Q(name=base))
    )
    if direct:
        return direct

    matches = []
    for task in (
        ProdTask.objects
        .select_related('dataset', 'prod_config')
        .exclude(overrides__isnull=True)
        .iterator()
    ):
        candidates = set()
        for output in task.outputs or [{}]:
            candidates.update(_progress_candidate_tasknames(task, output))
        if name in candidates or base in candidates:
            matches.append(task)
    return matches


def intake_direct_panda_task(panda_task, *, created_by='association_sweep'):
    """Auto-intake a directly submitted group.EIC production task.

    Commissioning/migration policy: everything lands in the catalog, whatever
    the submission path. For a PanDA task whose name has no PCS match, this
    creates the catalog structure the same way past-campaign assimilation
    does — Campaign from the version segment (lifecycle classified against
    the current campaign), Dataset from the name with the physics tag derived
    from the descriptive remainder (anchor tags elsewhere), anchor ProdConfig,
    and a ProdTask in status 'submitted' — then the normal association
    reconciler links the PanDA task to it.

    Returns ``(ProdTask|None, reason)``.
    """
    task_name = panda_name_from_physical_name(panda_task.get('taskname') or '')
    if not task_name.startswith('group.EIC.'):
        return None, 'not a group.EIC production taskname'
    parts = task_name.split('.')
    # group . EIC . <det_version: N.N.N> . <det_config> . <remainder...>
    if len(parts) < 8:
        return None, f'taskname too short to parse: {task_name!r}'
    det_version = '.'.join(parts[2:5])
    det_config = parts[5]
    remainder = '.'.join(parts[6:])
    if not _re.fullmatch(r'\d+\.\d+\.\d+', det_version) or not remainder:
        return None, f'unparseable taskname: {task_name!r}'

    physics, evgen, simu, reco, cfg, _ = _ensure_csvimport_anchors()
    current_camp = Campaign.objects.filter(lifecycle='current').first()
    current_v = _version_tuple(current_camp.name) if current_camp else None
    lc = 'future' if (current_v is not None
                      and _version_tuple(det_version) > current_v) else 'past'

    with transaction.atomic():
        campaign, _created = Campaign.objects.get_or_create(
            name=det_version,
            defaults={'lifecycle': lc,
                      'description': f'Auto-created on intake of direct PanDA '
                                     f'submissions for {det_version}',
                      'created_by': created_by},
        )

        beam = ''
        beam_match = _re.search(r'\.(\d+x\d+)(\.|$)', remainder)
        if beam_match:
            beam = beam_match.group(1)
        derived = derive_physics(remainder.replace('.', '/'), beam=beam)
        row_physics_tag = physics
        if derived is None:
            pass  # unresolved physics stays on the placeholder anchor
        elif derived.get('process') in ('BEAMGAS', 'SYNRAD'):
            row_physics_tag = _no_signal_physics_tag()
        else:
            row_physics_tag, _ = find_or_create_physics_tag(
                derived, created_by=created_by)

        did = f'group.EIC:{task_name}'
        dataset, _ds_created = Dataset.objects.get_or_create(
            did=did,
            defaults={
                'dataset_name': task_name,
                'scope': 'group.EIC',
                'detector_version': det_version,
                'detector_config': det_config,
                'campaign': campaign,
                'physics_tag': row_physics_tag,
                'evgen_tag': evgen,
                'simu_tag': simu,
                'reco_tag': reco,
                'description': 'Auto-intake of direct PanDA submission',
                'metadata': {
                    'source': {'kind': 'panda_taskname', 'location': task_name},
                    'direct_intake': {
                        'jeditaskid': panda_task.get('jeditaskid'),
                        'username': panda_task.get('username') or '',
                    },
                },
                'created_by': created_by,
            },
        )

        task, _t_created = ProdTask.objects.get_or_create(
            name=task_name,
            defaults={
                'status': 'submitted',
                'dataset': dataset,
                'prod_config': cfg,
                'campaign': campaign,
                'description': (f'Auto-intake of direct PanDA submission '
                                f'jediTaskID={panda_task.get("jeditaskid")}'),
                'created_by': created_by,
            },
        )
    return task, 'intaken'


def reconcile_panda_task_association(panda_task):
    """Create a PandaTasks row for an externally discovered PanDA task.

    Returns ``(ProdTask|None, PandaTasks|None, reason)``. Association is created
    only on a single exact match; ambiguous or unmatched names are reported and
    left untouched.
    """
    try:
        jedi_task_id = int(panda_task.get('jeditaskid'))
    except (TypeError, ValueError):
        return None, None, 'missing jeditaskid'
    task_name = panda_name_from_physical_name(panda_task.get('taskname') or '')
    if not task_name:
        return None, None, 'missing PanDA taskname'

    row = (
        PandaTasks.objects
        .select_related('prod_task', 'prod_task__dataset', 'prod_task__prod_config')
        .filter(jedi_task_id=jedi_task_id)
        .first()
    )
    if row:
        return row.prod_task, row, 'existing jediTaskID association'

    matches = _match_prod_tasks_for_panda_name(task_name)
    if not matches:
        return None, None, f'no exact PCS match for PanDA taskname {task_name!r}'
    if len(matches) > 1:
        return None, None, f'ambiguous PCS match for PanDA taskname {task_name!r}'

    task = matches[0]
    try_number = _try_number_from_physical_name(task.composed_name, task_name)
    if try_number is None:
        try_number = _try_number_from_physical_name(task.name, task_name)
    if try_number is None:
        try_number = _next_try_number(task)

    metadata = {
        'panda_status': panda_task.get('status') or '',
        'username': panda_task.get('username') or '',
        'workinggroup': panda_task.get('workinggroup') or '',
        'processingtype': panda_task.get('processingtype') or '',
    }
    with transaction.atomic():
        row = PandaTasks.objects.select_for_update().filter(task_name=task_name).first()
        if row and row.prod_task_id != task.pk:
            return None, None, f'PanDA taskname {task_name!r} already associated elsewhere'
        if row is None:
            if PandaTasks.objects.filter(prod_task=task, try_number=try_number).exists():
                try_number = _next_try_number(task)
            row = PandaTasks.objects.create(
                prod_task=task,
                try_number=try_number,
                task_name=task_name,
                out_ds=task_name,
                log_ds=_log_dataset_name(task_name),
                jedi_task_id=jedi_task_id,
                site=panda_task.get('site') or '',
                status_snapshot=panda_task.get('status') or '',
                association_source='dynamic_taskname_match',
                match_reason=f'exact PanDA taskname match: {task_name}',
                metadata=metadata,
            )
        elif row.jedi_task_id in (None, jedi_task_id):
            row.jedi_task_id = jedi_task_id
            row.status_snapshot = panda_task.get('status') or row.status_snapshot
            row.metadata = {**(row.metadata or {}), **metadata}
            row.save(update_fields=['jedi_task_id', 'status_snapshot', 'metadata', 'updated_at'])
        else:
            return None, None, (
                f'PanDA taskname {task_name!r} already records jediTaskID '
                f'{row.jedi_task_id}'
            )
    return task, row, 'created dynamic exact taskname association'


def _panda_progress_summaries(task_ids, tasknames):
    """Return read-only PanDA task summaries keyed by JEDI id and task name."""
    ids = sorted({_to_int(tid) for tid in task_ids if _to_int(tid)})
    names = sorted({str(n) for n in tasknames if n})
    if not ids and not names:
        return {}, {}, []

    from monitor_app.panda.constants import PANDA_SCHEMA
    from monitor_app.panda.queries import _get_task_job_counts
    from monitor_app.panda.sql import row_to_dict

    fields = [
        'jeditaskid', 'taskname', 'status', 'username',
        'creationdate', 'modificationtime', 'processingtype',
        'site', 'workinggroup', 'errordialog',
    ]
    clauses = []
    params = []
    if ids:
        clauses.append('"jeditaskid" IN (' + ','.join(['%s'] * len(ids)) + ')')
        params.extend(ids)
    if names:
        clauses.append('"taskname" IN (' + ','.join(['%s'] * len(names)) + ')')
        params.extend(names)
    sql = f"""
        SELECT {', '.join(f'"{field}"' for field in fields)}
        FROM "{PANDA_SCHEMA}"."jedi_tasks"
        WHERE {' OR '.join(clauses)}
        ORDER BY "jeditaskid" DESC
    """

    try:
        with connections['panda'].cursor() as cursor:
            cursor.execute(sql, params)
            rows = [row_to_dict(row, fields) for row in cursor.fetchall()]
    except Exception as e:
        return {}, {}, [f'PanDA task lookup failed: {e}']

    counts = _get_task_job_counts([row['jeditaskid'] for row in rows])
    by_id = {}
    by_name = {}
    for row in rows:
        tid = _to_int(row.get('jeditaskid'))
        c = counts.get(tid, {})
        nactive = _to_int(c.get('nactive'))
        nfinished = _to_int(c.get('nfinished'))
        nfailed = _to_int(c.get('nfailed'))
        nfinalfailed = _to_int(c.get('nfinalfailed'))
        nrunning = _to_int(c.get('nrunning'))
        total_jobs = nactive + nfinished + nfailed
        terminal_jobs = nfinished + nfailed
        row.update({
            'nactive': nactive,
            'nfinished': nfinished,
            'nfailed': nfailed,
            'nfinalfailed': nfinalfailed,
            'nrunning': nrunning,
            'maxattempt': c.get('maxattempt'),
            'total_jobs': total_jobs,
            'terminal_jobs': terminal_jobs,
            'processing_percent': (
                round(100 * terminal_jobs / total_jobs, 1) if total_jobs else None
            ),
            'final_failure_rate': (
                round(100 * nfinalfailed / (nfinalfailed + nfinished), 1)
                if (nfinalfailed + nfinished) else None
            ),
        })
        by_id[tid] = row
        by_name.setdefault(row.get('taskname') or '', row)
    return by_id, by_name, []


def refresh_campaign_progress_snapshot(campaign, *, generated_by='operator'):
    """Build and store the cached campaign progress view.

    Reads existing PCS output records and bounded PanDA summaries. It never
    queries Rucio; Rucio output facts come from ``ProdTask.overrides['outputs']``.
    """
    if campaign is None:
        raise ServiceError('No campaign selected for progress refresh.', status=400)

    tasks = list(
        ProdTask.objects
        .select_related('campaign', 'dataset', 'prod_config')
        .prefetch_related('panda_tasks')
        .filter(campaign=campaign)
        .order_by('-updated_at')
    )
    panda_ids = []
    panda_ids_by_task = {}
    for task in tasks:
        ids = [row.jedi_task_id for row in task.panda_tasks.all() if row.jedi_task_id]
        if task.panda_task_id and task.panda_task_id not in ids:
            ids.insert(0, task.panda_task_id)
        panda_ids_by_task[task.pk] = ids
        panda_ids.extend(ids)
    taskname_candidates = []
    candidate_names_by_task_output = {}
    for task in tasks:
        for index, output in enumerate(task.outputs or [{}]):
            candidates = _progress_candidate_tasknames(task, output)
            candidate_names_by_task_output[(task.pk, index)] = candidates
            taskname_candidates.extend(candidates)

    panda_by_id, panda_by_name, errors = _panda_progress_summaries(
        panda_ids, taskname_candidates)

    rows = {}
    for task in tasks:
        cfg = task.get_effective_config()
        data = cfg.get('data') or {}
        configured_jobs = _to_int(data.get('n_jobs'), 0) or None
        direct_panda = None
        for panda_id in panda_ids_by_task.get(task.pk, []):
            direct_panda = panda_by_id.get(_to_int(panda_id))
            if direct_panda:
                break
        outputs = []

        for index, output in enumerate(task.outputs):
            matched = direct_panda
            link = 'recorded PanDA task' if matched else ''
            if not matched:
                for candidate in candidate_names_by_task_output.get((task.pk, index), []):
                    matched = panda_by_name.get(candidate)
                    if matched:
                        link = 'task-name match'
                        break

            file_count = _progress_output_file_count(output)
            expected_jobs = None
            expected_source = ''
            if matched and matched.get('total_jobs'):
                expected_jobs = matched['total_jobs']
                expected_source = 'observed PanDA jobs'
            elif configured_jobs:
                expected_jobs = configured_jobs
                expected_source = 'configured jobs'

            pct = round(100 * file_count / expected_jobs, 1) if expected_jobs else None
            outputs.append({
                'did': output.get('did') or '',
                'stage': output.get('stage') or '',
                'file_count': file_count,
                'bytes': _to_int(output.get('bytes'), 0),
                'complete': bool(output.get('complete', True)),
                'checked_at': output.get('checked_at') or '',
                'expected_jobs': expected_jobs,
                'expected_source': expected_source,
                'completion_percent': pct,
                'completion_label': f'{pct:.1f}%' if pct is not None else '',
                'completion_width': min(100, pct) if pct is not None else 0,
                'processing': matched or {},
                'processing_backend': 'PanDA' if matched else '',
                'link': link,
            })

        rows[str(task.pk)] = {
            'task_id': task.pk,
            'task_name': task.composed_name,
            'configured_jobs': configured_jobs,
            'outputs': outputs,
            'has_processing': bool(direct_panda or any(o.get('processing') for o in outputs)),
        }

    snapshot = {
        'version': 1,
        'campaign': campaign.name,
        'generated_at': _timezone.now().isoformat(),
        'generated_by': generated_by,
        'task_count': len(tasks),
        'rows': rows,
        'errors': errors,
    }
    _cache.set(campaign_progress_snapshot_cache_key(campaign), snapshot, None)
    return {
        'campaign': campaign.name,
        'tasks': len(tasks),
        'errors': errors,
        'generated_at': snapshot['generated_at'],
    }


# ---------------------------------------------------------------------------
# Questionnaire
# ---------------------------------------------------------------------------

QUESTIONNAIRE_FIELD_MAP = {
    'submitted_at': (
        'Timestamp',
        'submitted_at',
    ),
    'description': (
        'Name the dataset, generator, and purpose',
        'description',
    ),
    'repository': (
        'Location of the repository with version control enforced per the input-processing guidelines (e.g. a tagged generator or steering-file repository)',
        'Provide the location of the repository with proper version control enforced according to the input processing guidelines. See https://github.com/eic/LambdaGen/tags (only steering files+metadata) or https://github.com/eic/DEMPgen/tags (generator+steering files+metadata) for example.',
        'repository',
    ),
    'contact': (
        'Who is the contact person for the dataset generation',
        'Who is the contact person for the dataset generation?',
        'contact',
    ),
    'nevents': (
        'How many events are requested',
        'How many events are you requesting?',
        'nevents',
    ),
    'benchmark': (
        'Time to simulate the first 100 events and disk space the output file occupies',
        'How much time does it take to simulate the first 100 events of the dataset and how much diskspace does the output file occupy?',
        'benchmark',
    ),
    'estimate': (
        'Total compute or storage required for the dataset, with justification for requests above 1% of the campaign budget (~120 core-years and ~35 TB per month)',
        'Use the answers to the last two questions to estimate how much total compute or storage will be required for the entire dataset. Overall campaign compute budget per month is ~120 Core Years and ~35 TB. For requests that are bigger than 1% of these values, provide justification.',
        'estimate',
    ),
}


def _row_value(row, aliases):
    lower = {str(k).strip().lower(): v for k, v in row.items()}
    for alias in aliases:
        key = alias.strip().lower()
        if key in lower:
            return '' if lower[key] is None else str(lower[key]).strip()
    return ''


def _parse_questionnaire_timestamp(value):
    if not value:
        raise ServiceError('Questionnaire row is missing Timestamp/submitted_at.')
    parsed = _parse_datetime(value)
    if parsed is None:
        for fmt in (
            '%m/%d/%Y %H:%M:%S',
            '%m/%d/%Y %H:%M',
            '%Y-%m-%d %H:%M:%S',
            '%Y-%m-%d %H:%M',
        ):
            try:
                parsed = _datetime.strptime(value, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        raise ServiceError(f'Could not parse questionnaire timestamp {value!r}.')
    if _timezone.is_naive(parsed):
        parsed = _timezone.make_aware(parsed, _timezone.get_current_timezone())
    return parsed


def _questionnaire_payload_from_row(row):
    payload = {
        field: _row_value(row, aliases)
        for field, aliases in QUESTIONNAIRE_FIELD_MAP.items()
    }
    payload['submitted_at'] = _parse_questionnaire_timestamp(payload['submitted_at'])
    return payload


def _questionnaire_content_hash(payload):
    content = {
        k: (payload[k].isoformat() if k == 'submitted_at' else payload[k])
        for k in (
            'submitted_at', 'description', 'repository', 'contact',
            'nevents', 'benchmark', 'estimate',
        )
    }
    raw = _json.dumps(content, sort_keys=True, separators=(',', ':'))
    return _hashlib.sha256(raw.encode('utf-8')).hexdigest()


@transaction.atomic
def questionnaire_intake(rows, *, source_url='', created_by='questionnaire_import'):
    """Idempotently mirror Google Form response rows into Questionnaire.

    The Google Form remains the user-facing authority. PCS keys on the form
    timestamp and updates the mirror only when the mirrored response content
    hash changes.
    """
    summary = {'created': 0, 'updated': 0, 'unchanged': 0, 'items': []}
    for idx, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise ServiceError(f'Questionnaire row {idx} is not an object.')
        payload = _questionnaire_payload_from_row(row)
        content_hash = _questionnaire_content_hash(payload)
        source_row = str(idx)
        defaults = {
            **payload,
            'source_url': source_url or '',
            'source_row': source_row,
            'content_hash': content_hash,
            'created_by': created_by,
        }
        questionnaire, created = Questionnaire.objects.get_or_create(
            submitted_at=payload['submitted_at'],
            defaults=defaults,
        )
        if created:
            action = 'created'
            summary['created'] += 1
        elif questionnaire.content_hash != content_hash:
            for field in (
                'description', 'repository', 'contact', 'nevents',
                'benchmark', 'estimate',
            ):
                setattr(questionnaire, field, payload[field])
            questionnaire.source_url = source_url or questionnaire.source_url
            questionnaire.source_row = source_row
            questionnaire.content_hash = content_hash
            questionnaire.save(update_fields=[
                'description', 'repository', 'contact', 'nevents',
                'benchmark', 'estimate', 'source_url', 'source_row',
                'content_hash', 'updated_at',
            ])
            action = 'updated'
            summary['updated'] += 1
        else:
            action = 'unchanged'
            summary['unchanged'] += 1
        summary['items'].append({'id': questionnaire.pk, 'action': action})
    return summary


def questionnaire_intake_csv(csv_text, *, source_url='', created_by='questionnaire_import'):
    if not csv_text:
        raise ServiceError('csv_text is required.')
    rows = _csv.DictReader(_io.StringIO(csv_text))
    return questionnaire_intake(
        list(rows), source_url=source_url, created_by=created_by)


def rebuild_questionnaire_match_cache(*, updated_by='questionnaire_match'):
    """Rebuild task-local questionnaire-match caches.

    ``Questionnaire.data['prod_matches']`` is the editable source of truth.
    Catalog and compose views read the inverse cache from
    ``ProdTask.overrides['questionnaire_matches']`` so page rendering never
    scans every questionnaire.

    Matches bind to physics configurations (CAMPAIGN_CONTINUUM.md —
    requests over physics configurations): each accepted match carries
    ``pc_anchor``, the composed name of the matched task's dataset,
    self-healed here on every rebuild; the cache attaches the match to
    every task whose dataset resolves to the same configuration, so a
    match recorded against one campaign's edition is inherited by every
    other edition of the same physics without re-matching. Unresolved
    configurations key uniquely and never fan out.
    """
    from .physics_config import physics_config_key
    tasks = list(ProdTask.objects.select_related(
        'dataset', 'dataset__physics_tag', 'dataset__evgen_tag',
        'dataset__background_tag').all())
    by_id = {task.pk: task for task in tasks}
    by_name = {}
    for task in tasks:
        if task.name:
            by_name[task.name] = task
        if task.composed_name:
            by_name[task.composed_name] = task

    # Configuration resolution: one key per composed identity, and the
    # task pks realizing each configuration across campaigns.
    key_by_name = {}
    task_pks_by_key = {}
    for task in tasks:
        if not task.dataset_id:
            continue
        name = task.composed_name
        if name not in key_by_name:
            key_by_name[name] = physics_config_key(task.dataset)['key']
        task_pks_by_key.setdefault(key_by_name[name], set()).add(task.pk)

    matches_by_task = {}
    questionnaire_count = 0
    accepted_count = 0
    unresolved_count = 0
    changed_questionnaires = []
    for questionnaire in Questionnaire.objects.all().only('id', 'data'):
        questionnaire_count += 1
        anchors_healed = False
        for match in (questionnaire.data or {}).get('prod_matches') or []:
            if not isinstance(match, dict):
                continue
            if (match.get('status') or 'accepted') != 'accepted':
                continue
            accepted_count += 1
            task = None
            task_id = match.get('task_id')
            if isinstance(task_id, int):
                task = by_id.get(task_id)
            elif str(task_id).isdigit():
                task = by_id.get(int(task_id))
            if task is None:
                task = by_name.get(match.get('task_name') or '')
            if task is None:
                task = by_name.get(match.get('legacy_name') or '')
            if task is None:
                unresolved_count += 1
                continue
            anchor = task.composed_name if task.dataset_id else ''
            if anchor and match.get('pc_anchor') != anchor:
                match['pc_anchor'] = anchor
                anchors_healed = True
            entry = {
                'questionnaire_id': questionnaire.pk,
                'confidence': match.get('confidence') or '',
                'reason': match.get('reason') or '',
                'matched_at': match.get('matched_at') or '',
            }
            target_pks = {task.pk}
            key = key_by_name.get(anchor)
            if key is not None:
                target_pks.update(task_pks_by_key.get(key, ()))
            for target_pk in target_pks:
                bucket = matches_by_task.setdefault(target_pk, [])
                if not any(e['questionnaire_id'] == questionnaire.pk
                           for e in bucket):
                    bucket.append(entry)
        if anchors_healed:
            changed_questionnaires.append(questionnaire)
    if changed_questionnaires:
        Questionnaire.objects.bulk_update(
            changed_questionnaires, ['data'], batch_size=200)

    now = _timezone.now()
    changed = []
    for task in tasks:
        overrides = dict(task.overrides or {})
        existing = overrides.get('questionnaire_matches') or []
        rebuilt = matches_by_task.get(task.pk, [])
        if existing == rebuilt:
            continue
        if rebuilt:
            overrides['questionnaire_matches'] = rebuilt
            overrides['questionnaire_matches_updated_at'] = now.isoformat()
            overrides['questionnaire_matches_updated_by'] = updated_by
        else:
            overrides.pop('questionnaire_matches', None)
            overrides.pop('questionnaire_matches_updated_at', None)
            overrides.pop('questionnaire_matches_updated_by', None)
        task.overrides = overrides
        task.updated_at = now
        changed.append(task)
    if changed:
        ProdTask.objects.bulk_update(changed, ['overrides', 'updated_at'], batch_size=200)
    return {
        'questionnaires': questionnaire_count,
        'accepted_matches': accepted_count,
        'resolved_matches': sum(len(v) for v in matches_by_task.values()),
        'unresolved_matches': unresolved_count,
        'tasks_updated': len(changed),
        'anchors_healed': len(changed_questionnaires),
    }


# ---------------------------------------------------------------------------
# Dataset
# ---------------------------------------------------------------------------

def dataset_intake(*, source_location, source_kind='csv_manifest',
                   scope='group.EIC.evgen', stage='evgen',
                   detector_version=None, detector_config=None,
                   physics_tag_label=None, evgen_tag_label=None,
                   simu_tag_label=None, reco_tag_label=None,
                   background_tag_label=None, sample_name='',
                   description='', created_by):
    """
    Idempotent intake of an external (e.g. EVGEN CSV manifest) Dataset.

    Idempotency key: ``(source.kind, source.location)``.

    Returns ``(dataset, was_created)``.
    """
    if not source_location:
        raise ServiceError('source_location is required')

    existing = Dataset.objects.filter(
        metadata__source__location=source_location,
        metadata__source__kind=source_kind,
    ).first()
    if existing:
        return existing, False

    required = {
        'detector_version': detector_version,
        'detector_config':  detector_config,
        'physics_tag':      physics_tag_label,
        'evgen_tag':        evgen_tag_label,
        'simu_tag':         simu_tag_label,
        'reco_tag':         reco_tag_label,
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        raise ServiceError(
            f'No existing Dataset for {source_kind}:{source_location}; '
            f'creation requires: {", ".join(missing)}'
        )

    tag_specs = {
        'physics_tag': (PhysicsTag, physics_tag_label),
        'evgen_tag':   (EvgenTag,   evgen_tag_label),
        'simu_tag':    (SimuTag,    simu_tag_label),
        'reco_tag':    (RecoTag,    reco_tag_label),
    }
    tags = {}
    for field, (model, label) in tag_specs.items():
        tag = model.objects.filter(tag_label=label).first()
        if not tag:
            raise ServiceError(f'{field} not found: {label}')
        tags[field] = tag

    if background_tag_label:
        bg = BackgroundTag.objects.filter(tag_label=background_tag_label).first()
        if not bg:
            raise ServiceError(f'background_tag not found: {background_tag_label}')
        tags['background_tag'] = bg

    campaign = Campaign.objects.filter(name=detector_version).first()
    ds = Dataset(
        scope=scope,
        detector_version=detector_version,
        detector_config=detector_config,
        campaign=campaign,
        sample_name=sample_name,
        description=description,
        metadata={
            'stage': stage,
            'source': {'kind': source_kind, 'location': source_location},
        },
        created_by=created_by,
        **tags,
    )
    try:
        ds.save()
    except Exception as e:
        raise ServiceError(str(e))
    return ds, True


# ---------------------------------------------------------------------------
# ProdTask
# ---------------------------------------------------------------------------

def prodtask_intake(*, payload, created_by):
    """
    Idempotent intake of a draft ProdTask.

    Idempotency key (one of, in payload):
        public_catalog_issue (preferred), or
        (public_catalog_csv_path, public_catalog_row_key)

    On match the existing task is updated (catalogue mapping merged
    into overrides; description optionally refreshed; input_dataset_did
    optionally set/updated). On no match, a draft task is created and
    requires ``name``, ``dataset`` (DID or name), and ``prod_config``
    (name) in the payload.

    Returns ``(task, was_created)``.
    """
    key_issue = payload.get('public_catalog_issue')
    key_csv_path = payload.get('public_catalog_csv_path')
    key_row = payload.get('public_catalog_row_key')
    if not key_issue and not (key_csv_path and key_row):
        raise ServiceError(
            'Idempotency key required: public_catalog_issue or '
            '(public_catalog_csv_path, public_catalog_row_key)'
        )

    if key_issue:
        existing = ProdTask.objects.filter(
            overrides__public_catalog_issue=key_issue
        ).first()
    else:
        existing = ProdTask.objects.filter(
            overrides__public_catalog_csv_path=key_csv_path,
            overrides__public_catalog_row_key=key_row,
        ).first()

    new_catalog = {k: payload[k] for k in PUBLIC_CATALOG_KEYS if k in payload}
    new_input_did = payload.get('input_dataset_did')

    if existing:
        ov = dict(existing.overrides or {})
        ov.update(new_catalog)
        if new_input_did:
            ov['input_dataset_did'] = new_input_did
        existing.overrides = ov
        update_fields = ['overrides', 'updated_at']
        if payload.get('description') is not None:
            existing.description = payload['description']
            update_fields.append('description')
        existing.save(update_fields=update_fields)
        return existing, False

    name = payload.get('name')
    dataset_handle = payload.get('dataset')
    config_handle = payload.get('prod_config')
    missing = [k for k, v in [('name', name),
                              ('dataset', dataset_handle),
                              ('prod_config', config_handle)]
               if not v]
    if missing:
        raise ServiceError(
            f'Creating a new task requires: {", ".join(missing)}'
        )
    try:
        output_ds = resolve_dataset(
            dataset_handle,
            queryset=Dataset.objects.order_by('block_num', 'id'),
        )
    except Dataset.DoesNotExist:
        raise ServiceError(f'Output dataset not found: {dataset_handle}')
    config = ProdConfig.objects.filter(name=config_handle).first()
    if not config:
        raise ServiceError(f'ProdConfig not found: {config_handle}')

    ov = dict(new_catalog)
    if new_input_did:
        ov['input_dataset_did'] = new_input_did
    task = ProdTask.objects.create(
        name=name,
        description=payload.get('description', ''),
        status='draft',
        dataset=output_ds,
        prod_config=config,
        overrides=ov or None,
        created_by=created_by,
    )
    return task, True


def prodtask_link_input(*, task, did=None, dids=None):
    """
    Link input Dataset(s) to a ProdTask via overrides JSON. Provide
    one of ``did`` (single) or ``dids`` (list), not both. Linked
    Datasets must already exist; this never creates Datasets.
    """
    if did and dids:
        raise ServiceError('Provide one of did or dids, not both')
    if not did and not dids:
        raise ServiceError('did or dids is required')
    targets = [did] if did else list(dids)
    found = set(Dataset.objects.filter(did__in=targets)
                .values_list('did', flat=True))
    missing = [d for d in targets if d not in found]
    if missing:
        raise ServiceError(f'Dataset(s) not found: {missing}')
    ov = dict(task.overrides or {})
    if did:
        ov['input_dataset_did'] = did
        ov.pop('input_dataset_dids', None)
    else:
        ov['input_dataset_dids'] = list(dids)
        ov.pop('input_dataset_did', None)
    task.overrides = ov
    task.save(update_fields=['overrides', 'updated_at'])
    return task


def _known_prodtask_statuses():
    """Universe of legal ProdTask status values, derived from the
    transition map rather than a CharField choices enum."""
    known = set(PRODTASK_TRANSITIONS.keys())
    for trans in PRODTASK_TRANSITIONS.values():
        known.update(trans)
    return known


def resolve_prodtask(name, queryset=None):
    """Resolve a ProdTask by its identity, the way every task URL and the
    ``?name=`` API endpoints key on it. First match wins:

      1. the composed tag name (``dataset.composed_name``) — the canonical
         identity, an indexed lookup on the stored column;
      2. the stored ``name`` — a legacy csv_import slash path or a native name
         (also indexed; ``name`` is unique);
      3. a bare integer pk — tolerated for a stale ``/tasks/<pk>/`` link inbound
         only. It is never emitted; a detail view 301s it to the composed name.

    Raises ``ProdTask.DoesNotExist`` if nothing matches, so it is a drop-in for
    ``queryset.get(name=...)``.
    """
    qs = ProdTask.objects.all() if queryset is None else queryset
    key = str(name)
    t = qs.filter(dataset__composed_name=key).first()
    if t is not None:
        return t
    t = qs.filter(name=key).first()
    if t is not None:
        return t
    if key.isdigit():
        t = qs.filter(pk=int(key)).first()
        if t is not None:
            return t
    raise ProdTask.DoesNotExist(f"No ProdTask matches {name!r}")


def resolve_dataset(name, queryset=None):
    """Resolve a Dataset by its identity, the peer of ``resolve_prodtask``.
    First match wins:

      1. the composed tag name (``composed_name``) — the canonical identity,
         an indexed lookup on the stored column;
      2. the Rucio ``did`` (``scope:name.bN``);
      3. the stored ``dataset_name`` — a legacy csv_import hash or native name.

    Raises ``Dataset.DoesNotExist`` if nothing matches, so it is a drop-in for
    ``queryset.get(...)``.
    """
    qs = Dataset.objects.all() if queryset is None else queryset
    key = str(name)
    for lookup in ('composed_name', 'did', 'dataset_name'):
        d = qs.filter(**{lookup: key}).first()
        if d is not None:
            return d
    raise Dataset.DoesNotExist(f"No Dataset matches {name!r}")


def prodtask_readiness_problems(task):
    """Reasons a task is NOT ready to lock/submit; empty list = ready.

    Checks what a valid PanDA submission needs, independent of how the task was
    built (composed tags or imported catalog metadata):

    - **Output.** The task must produce a physics output — at least one of
      ``copy_reco`` / ``copy_full``. A task that copies neither (logs only)
      produces nothing worth submitting.
    - **EVGEN input.** The client-API EVGEN submit path needs at least one
      matched JLab Rucio EVGEN input. Without it the submission spec builder
      refuses to emit a manifest.
    - **Physics is really bound.** An imported catalog row carries its real beam
      in its metadata (``overrides['csv_import']['filters']['beam']``, e.g.
      ``18x275``). The import pins every such row to one placeholder anchor
      physics tag, so if the bound tag's beam disagrees with the catalog beam,
      the physics is a placeholder and the submission artifact would carry the
      wrong beam. The physics automatch (``find_or_create_physics_tag`` wired
      into the importer) binds the right tag, so this clears on reload. Background
      rows are exempt: they stay parked on the anchor pending the ``k`` background
      tag type, so their beam mismatch is expected, not an error.
    """
    problems = []
    cfg = task.get_effective_config()
    if not (cfg.get('copy_reco') or cfg.get('copy_full')):
        problems.append('No physics output configured (enable copy of reco or full).')
    if not task.has_input:
        problems.append('No matched Rucio EVGEN input (run the EVGEN matcher first).')

    ds = task.dataset
    if cfg.get('bg_mixing') and ds:
        bg_params = {}
        if ds.background_tag_id:
            bg_params = ds.background_tag.parameters or {}
        evgen_params = ds.evgen_tag.parameters or {}
        bg_tag_prefix = bg_params.get('bg_tag_prefix') or evgen_params.get('bg_tag_prefix')
        bg_files = bg_params.get('bg_files') or evgen_params.get('bg_files')
        if not ds.background_tag_id and not (bg_tag_prefix or bg_files):
            problems.append(
                'Background mixing is enabled but no background tag or legacy '
                'EvGen background parameters are attached.')
        if ds.background_tag_id and not (bg_tag_prefix or bg_files):
            problems.append(
                'Background mixing is enabled but the background tag has no '
                'bg_tag_prefix or bg_files parameter.')

    csv_filters = (((task.overrides or {}).get('csv_import') or {})
                   .get('filters') or {})
    catalog_beam = csv_filters.get('beam') or ''
    is_background = csv_filters.get('physics') == 'BACKGROUNDS'
    if catalog_beam and not is_background and task.dataset_id and task.dataset.physics_tag_id:
        pt = task.dataset.physics_tag
        p = pt.parameters or {}
        e = str(p.get('beam_energy_electron', '')).strip()
        h = str(p.get('beam_energy_hadron', '')).strip()
        tag_beam = f'{e}x{h}' if e and h else ''
        if tag_beam and tag_beam != catalog_beam:
            problems.append(
                f'Wrong physics tag: this sample is {catalog_beam} but it is '
                f'tagged {pt.tag_label} ({tag_beam}). Assign the matching '
                f'physics tag.')
    return problems


def prodtask_set_status(*, task, new_status):
    """Lifecycle transition with rule enforcement."""
    valid = _known_prodtask_statuses()
    if new_status not in valid:
        raise ServiceError(
            f'Invalid status. Choose from: {", ".join(sorted(valid))}'
        )
    allowed = PRODTASK_TRANSITIONS.get(task.status, set())
    if new_status != task.status and new_status not in allowed:
        raise ServiceError(
            f'Cannot transition from {task.status!r} to {new_status!r}. '
            f'Allowed from {task.status!r}: '
            f'{sorted(allowed) or "(terminal)"}'
        )
    # Readiness gate: locking (→ ready) requires a submittable task. One
    # chokepoint for the lock action, the detail-page lock, and REST/MCP
    # set-status. See prodtask_readiness_problems.
    if new_status == 'ready' and task.status != 'ready':
        problems = prodtask_readiness_problems(task)
        if problems:
            raise ServiceError('Cannot lock — task is not ready: ' + ' '.join(problems))

    task.status = new_status
    task.save(update_fields=['status', 'updated_at'])
    return task


def prodtask_apply_request(task, request, *, save=True):
    """Copy seed fields from ``request`` onto ``task`` (request → task).

    Mutates ``task.request`` and the fields listed in
    ``REQUEST_TO_TASK_COPY_FIELDS``. Saves by default. After this call
    both rows are independently mutable; a later request edit does
    *not* automatically resync to the task. Call this again explicitly
    if a resync is desired.
    """
    if request is None:
        raise ServiceError('request is required')
    task.request = request
    for field in REQUEST_TO_TASK_COPY_FIELDS:
        setattr(task, field, getattr(request, field))
    if save:
        task.save(update_fields=['request'] + list(REQUEST_TO_TASK_COPY_FIELDS)
                                + ['updated_at'])
    return task


# ---------------------------------------------------------------------------
# Campaign
# ---------------------------------------------------------------------------

def campaign_set_current(campaign):
    """Atomically promote ``campaign`` to lifecycle='current'.

    Any other Campaign currently at 'current' is demoted to 'past'.
    No-op if ``campaign`` is already current. Service-layer enforcement
    (no DB constraint) — direct admin saves can violate the invariant.
    """
    with transaction.atomic():
        if campaign.lifecycle == 'current':
            return campaign
        Campaign.objects.filter(lifecycle='current').exclude(pk=campaign.pk) \
                        .update(lifecycle='past')
        campaign.lifecycle = 'current'
        campaign.save(update_fields=['lifecycle', 'updated_at'])
    return campaign


def campaign_clone_to_new(source, *, name, created_by,
                          description='', lifecycle='future'):
    """Create a new Campaign whose ``clone_of`` points to ``source``.

    The new campaign is blank — tasks are not cloned by this helper.
    Task cloning is a separate operation (potentially per-task, as
    dataset rebinding for the new campaign's detector_version is
    non-trivial). Use ``campaign_set_current`` separately to promote.
    """
    if Campaign.objects.filter(name=name).exists():
        raise ServiceError(f'Campaign name already in use: {name}')
    if lifecycle not in {'past', 'current', 'future'}:
        raise ServiceError(f'Invalid lifecycle: {lifecycle!r}')
    new_c = Campaign.objects.create(
        name=name,
        lifecycle=lifecycle,
        description=description,
        clone_of=source,
        created_by=created_by,
    )
    return new_c


# ---------------------------------------------------------------------------
# Submission state changes
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Default-datasets CSV import (Sakib's epic-prod docs/_data/datasets.csv)
# ---------------------------------------------------------------------------

# Local path to the cloned epic-prod CSV on swf-testbed.
DEFAULT_DATASETS_CSV_PATH = (
    '/data/wenauseic/github/epic-prod/docs/_data/datasets.csv'
)


_YESNO_RECOGNISED = {'', 'yes', 'y', 'no', 'n', 'true', 'false', '0', '1'}


def _yesno(value):
    """Parse Sakib's Yes/No into a boolean; everything else -> False.

    Logs a WARN when a non-empty value is unrecognised (e.g. 'Maybe'),
    so silent drops surface in the log. NO-SILENT-FAILURES rule.
    """
    s = str(value or '').strip().lower()
    if s and s not in _YESNO_RECOGNISED:
        _log.warning('_yesno: unrecognised value %r treated as False', value)
    return s in ('yes', 'y', 'true', '1')


_GH_EIC_RELEASE_RE = _re.compile(r'https?://github\.com/eic/([^/]+)/releases\b', _re.I)
_GH_OTHER_RELEASE_RE = _re.compile(r'https?://github\.com/([^/]+)/([^/]+)/releases\b', _re.I)
_GL_EIC_RE   = _re.compile(r'https?://gitlab\.com/eic/([^?#]+?)/-/', _re.I)
_GL_OTHER_RE = _re.compile(r'https?://gitlab\.com/([^?#]+?)/-/', _re.I)

# Owner / group prefixes to strip from the extracted evgen identifier.
# They're consistent organisational namespaces, not generator names, so
# leaving them in just makes the filter values noisy and prevents merging
# of variants. Add new prefixes here as new sources appear.
_EVGEN_STRIP_PREFIXES = ('JeffersonLab/', 'mceg/')


def _extract_evgen(value):
    """Derive a short evgen identifier from a Generator/Dataset Version cell.

    - github.com/eic/<repo>/releases/...            -> '<repo>'
    - github.com/<owner>/<repo>/releases/... (non-eic) -> '<owner>/<repo>'
    - gitlab.com/eic/<path>/-/...                   -> '<path>'  (collapses
                                                       tag variants of the
                                                       same project under
                                                       one filter value)
    - gitlab.com/<path>/-/... (non-eic)             -> '<path>'
    - plain strings ('starlight', 'Pythia 8', ...)  -> unchanged

    Stored at ingest in overrides.csv_import.evgen so the catalog can
    filter and group by generator without re-parsing per render. The
    catalog will eventually bind these to canonical EvgenTag entries.
    """
    s = str(value or '').strip()
    if not s:
        return ''
    out = s
    m = _GH_EIC_RELEASE_RE.match(s)
    if m:
        out = m.group(1)
    elif (m := _GH_OTHER_RELEASE_RE.match(s)):
        out = f'{m.group(1)}/{m.group(2)}'
    elif (m := _GL_EIC_RE.match(s)):
        out = m.group(1)
    elif (m := _GL_OTHER_RE.match(s)):
        out = m.group(1)
    for prefix in _EVGEN_STRIP_PREFIXES:
        if out.startswith(prefix):
            return out[len(prefix):]
    return out


def _parse_event_count(value):
    """Parse Sakib's event-count strings ('400k', '1M', '2.75M', plain
    integers) into an absolute event count.

    Returns None for empty input (legitimate); WARN-logs and returns
    None for non-empty unparseable input. NO-SILENT-FAILURES rule —
    the previous strict int() parse dropped every populated row in
    the CSV without surfacing it anywhere.
    """
    raw = value
    s = str(value or '').strip().replace(',', '').replace(' ', '')
    if not s:
        return None
    mult = 1
    if s[-1] in ('k', 'K'):
        mult, s = 1_000, s[:-1]
    elif s[-1] in ('m', 'M'):
        mult, s = 1_000_000, s[:-1]
    elif s[-1] in ('b', 'B', 'g', 'G'):
        mult, s = 1_000_000_000, s[:-1]
    try:
        return int(round(float(s) * mult))
    except (TypeError, ValueError):
        _log.warning('_parse_event_count: unparseable value %r', raw)
        return None


def _possible(value):
    """Truthy unless explicitly negative — 'Maybe' counts as yes.
    Used for pTDR ('possible pre-TDR use') and Other Use, where any
    non-empty, non-No marker indicates the use applies."""
    s = str(value or '').strip().lower()
    return bool(s) and s not in ('no', 'n', 'false', '0')


def _csvimport_slug(dataset_path, gen_version):
    """Short stable slug derived from the (path, generator) idempotency key."""
    key = f'{dataset_path}|{gen_version}'.encode()
    return _hashlib.sha1(key).hexdigest()[:12]


# Common prefix on every catalog source path; carries no distinguishing
# information, so it is stripped from the human-readable task name (and the
# compose title strips it the same way).
EPIC_VOLATILE_PREFIX = '/volatile/eic/EPIC/'


def _task_name_from_path(path):
    """Human-readable ProdTask.name from a catalog source path: the path with
    the common ``/volatile/eic/EPIC/`` prefix stripped (matching the compose
    title). Paths are unique per catalog row. Returns '' for an empty path so
    the caller can fall back to the slug name."""
    p = (path or '').strip()
    if p.startswith(EPIC_VOLATILE_PREFIX):
        p = p[len(EPIC_VOLATILE_PREFIX):]
    return p


def _ensure_csvimport_anchors():
    """Resolve the placeholder Dataset-FK targets used by CSV-imported rows.

    Returns ``(physics_tag, evgen_tag, simu_tag, reco_tag, prod_config,
    campaign)``. All must already exist; this is a lookup, not a
    creator. ``PROTECT`` FKs from Dataset to Tag and from ProdTask to
    ProdConfig mean we cannot synthesize ad hoc — we pin to whatever's
    already locked in the DB. The prod team can replace these per-task
    when they bind a real Dataset/Config to a CSV-imported task.
    """
    def first_locked(model, label):
        t = model.objects.order_by('tag_number').first()
        if not t:
            raise ServiceError(f'No {label} tag available for CSV import')
        return t

    physics = first_locked(PhysicsTag, 'physics')
    evgen = first_locked(EvgenTag, 'evgen')
    simu = first_locked(SimuTag, 'simu')
    reco = first_locked(RecoTag, 'reco')

    cfg = (ProdConfig.objects.filter(name__icontains='26.02.0 Standard').first()
           or ProdConfig.objects.first())
    if not cfg:
        raise ServiceError('No ProdConfig available for CSV import anchor')

    campaign = Campaign.objects.filter(lifecycle='current').order_by('-updated_at').first()
    if not campaign:
        raise ServiceError('No current Campaign for CSV import')

    return physics, evgen, simu, reco, cfg, campaign


# Process -> physics category digit, for numbering NEWLY created tags
# (number = digit*1000 + global suffix). Existing tags are reused by their
# physics parameters and keep their historically-assigned numbers/categories;
# the seeded set predates this map and is internally inconsistent — renumbering
# it is a separate cleanup, deliberately out of scope here.
_PROCESS_CATEGORY = {
    'SINGLE': 1,
    'DIS': 2, 'DIS_NC': 2, 'DIS_CC': 2, 'DDIS': 2,
    'DVCS': 3, 'DDVCS': 3, 'TCS': 3,
    'SIDIS': 4, 'SIDIS_D0': 4, 'SIDIS_DIJET': 4, 'SIDIS_Lc': 4,
    'DEMP': 5, 'DVMP': 5, 'MESON_SF': 5,
    'DIFFRACTIVE_JPSI': 5, 'DIFFRACTIVE_PHI': 5, 'DIFFRACTIVE_RHO': 5,
    'PHOTOPRODUCTION_JPSI': 5, 'UPSILON': 5,
    'UCHANNEL_PI0': 5, 'UCHANNEL_RHO': 5, 'ALP': 5,
}


#: Every physics axis that defines a tag's identity. Matching compares the full
#: set (a missing axis counts as ''), so a tag is reused only when every physics
#: axis agrees — the old coarse (process, beam) key is what collapsed Q2 range,
#: decay/charge, beam config, and the rest onto the first-created tag.
_PHYSICS_MATCH_FIELDS = (
    'process', 'beam_energy_electron', 'beam_energy_hadron',
    'beam_species', 'nucleon', 'q2_range', 'decay_mode', 'hadron_charge',
    'helicity', 'polarization', 'coherence', 'model', 'beam_config',
    'state', 'mechanism', 'final_state', 'channel', 'mass',
    'particle', 'gun_energy',
)


def _physics_key(params):
    """Identity tuple over the full physics axis set (a missing axis == '')."""
    return tuple((params or {}).get(f, '') for f in _PHYSICS_MATCH_FIELDS)


def find_or_create_physics_tag(derived, *, created_by='csv_import', dry_run=False):
    """Resolve the physics tag whose full parameter set equals ``derived``.

    Matching keys on the complete physics axis set, not the ``(process, beam)``
    subset: two compositions differing in any axis (Q2 range, species, decay,
    beam config, ...) are different tags. A coarse DB filter on ``process``
    narrows candidates; the full set is compared in Python so a tag stored with
    only the axes it carries still matches (a missing axis counts as ''). Among
    matches a locked tag is preferred, then the lowest number. A miss creates a
    new *draft* tag, numbered via ``PhysicsTag.allocate_next`` and the category
    map; locking is a deliberate later step at submission prep, not here.

    Returns ``(tag, action)`` with action ``'reuse'`` | ``'create'``. In
    ``dry_run`` nothing is written and a ``'create'`` returns ``(None, 'create')``.
    """
    process = derived.get('process')
    want = _physics_key(derived)
    matches = sorted(
        (t for t in PhysicsTag.objects.filter(parameters__process=process)
         if _physics_key(t.parameters) == want),
        key=lambda t: (0 if t.status == 'locked' else 1, t.tag_number),
    )
    if matches:
        return matches[0], 'reuse'

    digit = _PROCESS_CATEGORY.get(process)
    if digit is None:
        raise ServiceError(f'No physics category mapping for process {process!r}')
    if dry_run:
        return None, 'create'
    category = PhysicsCategory.objects.get(digit=digit)
    tag = PhysicsTag(
        tag_number=PhysicsTag.allocate_next(category),
        category=category,
        status='draft',
        parameters=derived,
        created_by=created_by,
    )
    tag.save()
    return tag, 'create'


def _no_signal_physics_tag():
    """The locked, signal-free physics tag (``p6001``) that standalone background
    datasets name in the physics slot. Seeded by migration 0010."""
    tag = PhysicsTag.objects.filter(tag_label='p6001').first()
    if not tag:
        raise ServiceError('No-signal physics tag p6001 is missing; run migrations.')
    return tag


#: Background-tag dedup key — the sample-defining params.
_BG_MATCH_FIELDS = ('background_type', 'bg_source', 'bg_mechanism', 'bg_generator',
                    'beam_energy_electron', 'beam_energy_hadron')


def _background_description(p):
    """Comma-joined description synthesised from background-tag parameters:
    generator, type, beam energy, source, mechanism — each included only when
    present. Single-beam samples show just the present energy."""
    e = p.get('beam_energy_electron', 'N/A')
    h = p.get('beam_energy_hadron', 'N/A')
    if e != 'N/A' and h != 'N/A':
        beam = f'{e}x{h} GeV'
    elif e != 'N/A':
        beam = f'{e} GeV'
    elif h != 'N/A':
        beam = f'{h} GeV'
    else:
        beam = ''
    parts = [
        p.get('bg_generator', ''),
        p.get('background_type', ''),
        beam,
        p.get('bg_source', ''),
        p.get('bg_mechanism', ''),
    ]
    return ', '.join(x for x in parts if x)


def find_or_create_background_tag(params, *, created_by='csv_import', dry_run=False):
    """Resolve the background (k) tag for derived background params.

    Mirrors ``find_or_create_physics_tag``: match by the sample-defining
    parameters, prefer a locked tag then the lowest number, or create a new
    *draft* tag. Returns ``(tag, action)`` with action ``'reuse'`` | ``'create'``.
    """
    match = {f'parameters__{k}': params.get(k, '') for k in _BG_MATCH_FIELDS}
    matches = sorted(
        BackgroundTag.objects.filter(**match),
        key=lambda t: (0 if t.status == 'locked' else 1, t.tag_number),
    )
    if matches:
        tag = matches[0]
        # Backfill a synthesised description if missing (annotation, not a
        # reproducibility field) — never overwrite an existing one.
        if not tag.description and not dry_run:
            tag.description = _background_description(params)
            tag.save(update_fields=['description', 'updated_at'])
        return tag, 'reuse'
    if dry_run:
        return None, 'create'
    tag = BackgroundTag(
        tag_number=BackgroundTag.allocate_next(),
        status='draft',
        description=_background_description(params),
        parameters=params,
        created_by=created_by,
    )
    tag.save()
    return tag, 'create'


def find_or_create_evgen_tag(params, *, created_by='csv_import', dry_run=False):
    """Resolve the evgen (e) tag for derived ``{generator, generator_version}``
    params. Mirrors ``find_or_create_physics_tag``: match by (generator,
    generator_version), prefer locked then lowest number, or create a new *draft*
    tag. Returns ``(tag, action)`` with action ``'reuse'`` | ``'create'``."""
    match = {
        'parameters__generator': params.get('generator', ''),
        'parameters__generator_version': params.get('generator_version', ''),
    }
    # Radiative corrections split an otherwise-identical generator into distinct
    # tags; match on it only when the derivation set it, so non-radiative
    # generators keep matching by (generator, generator_version) alone.
    if params.get('radiative'):
        match['parameters__radiative'] = params['radiative']
    matches = sorted(
        EvgenTag.objects.filter(**match),
        key=lambda t: (0 if t.status == 'locked' else 1, t.tag_number),
    )
    if matches:
        return matches[0], 'reuse'
    if dry_run:
        return None, 'create'
    tag = EvgenTag(
        tag_number=EvgenTag.allocate_next(),
        status='draft',
        description=f"{params.get('generator', '')} {params.get('generator_version', '')}".strip(),
        parameters=params,
        created_by=created_by,
    )
    tag.save()
    return tag, 'create'


def import_default_datasets_csv(csv_path=None, *, created_by='csv_import'):
    """Import Sakib's epic-prod datasets.csv into the catalog.

    Each CSV row becomes (idempotently):
      - one Dataset (placeholder tags + ``26.02.0`` campaign labels,
        full row preserved in ``metadata['csv_import']``),
      - one ProdTask (status='draft', linked to the Dataset, the
        anchor ProdConfig, and the current Campaign) — imported rows enter
        the editable production lifecycle directly.

    Idempotency key per row: ``(Dataset Path, Generator/Dataset Version)``.
    Re-running updates the existing rows in place.

    Returns dict::

        {'rows': int, 'created': int, 'updated': int, 'errors': [str, ...]}
    """
    path = csv_path or DEFAULT_DATASETS_CSV_PATH
    physics, evgen, simu, reco, cfg, campaign = _ensure_csvimport_anchors()

    with open(path, newline='') as f:
        rows = list(_csv.DictReader(f))

    summary = {'rows': len(rows), 'created': 0, 'updated': 0,
               'requests_created': 0, 'requests_updated': 0,
               'tag_actions': {}, 'errors': []}

    with transaction.atomic():
        for i, row in enumerate(rows, 1):
            ds_path = (row.get('Dataset Path') or '').strip()
            gen_ver = (row.get('Generator/Dataset Version') or '').strip()
            if not ds_path and not gen_ver:
                summary['errors'].append(f'row {i}: empty path and gen_version, skipped')
                continue

            slug = _csvimport_slug(ds_path, gen_ver)
            dataset_name = f'csv_import.{slug}'
            # Human-readable task name = the source path (the row's title), not
            # an opaque hash, so URLs read clearly and the name can't be mistaken
            # for the importer firing. Falls back to the slug name for a path-less
            # (gen_version-only) row. The Rucio DID stays slug-based on
            # dataset_name — its charset rules are a separate concern.
            task_name = _task_name_from_path(ds_path) or dataset_name

            # Derive the real physics from the path and resolve its locked tag,
            # replacing the placeholder anchor. Backgrounds resolve to the
            # signal-free p6001 physics tag plus a derived k background tag; an
            # unrecognizable path keeps the anchor and is surfaced in errors
            # rather than silently mis-tagged.
            filters = _extract_csv_filters(ds_path, 'epic_craterlake')
            derived = derive_physics(task_name, beam=filters.get('beam', ''))
            is_background = bool(derived) and derived.get('process') in ('BEAMGAS', 'SYNRAD')
            angular_range = single_particle_angle(task_name)
            row_physics_tag = physics
            row_background_tag = None
            if derived is None:
                summary['errors'].append(
                    f'row {i}: path {task_name!r} is not a recognizable EVGEN '
                    f'entry; left on placeholder tag {physics.tag_label}')
            elif is_background:
                # Standalone background: physics slot is the signal-free p6001
                # sentinel; the background config itself becomes a k tag.
                row_physics_tag = _no_signal_physics_tag()
                bg_params = derive_background(task_name)
                if bg_params:
                    row_background_tag, bg_action = find_or_create_background_tag(
                        bg_params, created_by=created_by)
                    bg_key = f'bg-{bg_action}'
                    summary['tag_actions'][bg_key] = summary['tag_actions'].get(bg_key, 0) + 1
            else:
                row_physics_tag, action = find_or_create_physics_tag(
                    derived, created_by=created_by)
                summary['tag_actions'][action] = summary['tag_actions'].get(action, 0) + 1

            # Resolve the generator (evgen) from path/gen_version. Unresolved
            # (ambiguous/underspecified) rows keep the placeholder anchor and are
            # left for manual association — never guessed. See the curated
            # derive_evgen and docs (campaign->tag mapping).
            row_evgen_tag = evgen
            ev_params = derive_evgen(task_name, gen_ver)
            if ev_params:
                row_evgen_tag, ev_action = find_or_create_evgen_tag(
                    ev_params, created_by=created_by)
                ev_key = f'evgen-{ev_action}'
                summary['tag_actions'][ev_key] = summary['tag_actions'].get(ev_key, 0) + 1

            raw_priority = (row.get('Priority') or '').strip()
            try:
                priority = int(raw_priority) if raw_priority else None
            except (TypeError, ValueError):
                _log.warning('csv_import row %d: unparseable Priority %r', i, raw_priority)
                priority = None
            nevents = _parse_event_count(row.get('Number of Events'))

            metadata = {
                'stage': 'evgen',
                'source': {'kind': 'csv_manifest', 'location': ds_path},
                'csv_import': {k: (v or '').strip() for k, v in row.items()},
            }
            if gen_ver:
                metadata['source']['gen_version'] = gen_ver

            # Single-particle samples share a (particle, energy) tag and differ
            # only by polar-angle range — the sample-variant discriminator that
            # composes into the dataset name. Empty for everything else (the
            # tags are a unique identity there). Same value as the per-task
            # angular_range above.
            ds_sample_name = angular_range
            ds = Dataset.objects.filter(dataset_name=dataset_name, block_num=1).first()
            if ds:
                ds.description = (row.get('Description') or '').strip()
                ds.metadata = metadata
                ds.campaign = campaign
                ds.physics_tag = row_physics_tag
                ds.evgen_tag = row_evgen_tag
                ds.background_tag = row_background_tag
                ds.sample_name = ds_sample_name
                ds.save()
                ds_created = False
            else:
                ds = Dataset(
                    dataset_name=dataset_name,
                    scope='group.EIC',
                    detector_version='26.02.0',
                    detector_config='epic_craterlake',
                    campaign=campaign,
                    physics_tag=row_physics_tag, evgen_tag=row_evgen_tag,
                    simu_tag=simu, reco_tag=reco,
                    background_tag=row_background_tag,
                    sample_name=ds_sample_name,
                    description=(row.get('Description') or '').strip(),
                    metadata=metadata,
                    created_by=created_by,
                )
                ds.save()
                ds_created = True

            task_defaults = dict(
                description=(row.get('Description') or '').strip(),
                # Imported catalog rows enter the editable production lifecycle
                # directly — turning Sakib's static default_datasets.csv list
                # into the living, actionable current-campaign catalog is the
                # whole point of the import. (The legacy 'csv_import' holding
                # status + per-row Adopt step were retired; see migration 0008.)
                status='draft',
                dataset=ds,
                prod_config=cfg,
                campaign=campaign,
                requestor=(row.get('DSC or PWG') or '').strip().upper(),
                priority=priority,
                pre_tdr_use=_possible(row.get('Pre-TDR Use')),
                early_science_use=_yesno(row.get('Early Science Use')),
                other_use=_possible(row.get('Other Use')),
                new_request=_yesno(row.get('New Request')),
                overrides={
                    'csv_import': {
                        'background': (row.get('Background') or '').strip(),
                        'nevents': nevents,
                        'issue_url': (row.get('Issue') or '').strip(),
                        'gen_version': gen_ver,
                        'evgen': _extract_evgen(gen_ver),
                        'other_use_text': (row.get('Other Use') or '').strip(),
                        'filters': filters,
                        'angular_range': angular_range,
                    },
                },
                created_by=created_by,
            )

            existing = ProdTask.objects.filter(name=task_name).first()
            if existing:
                # A re-import refreshes the catalog data but never moves the
                # task in its lifecycle — whatever state an operator has
                # reached (draft/ready/submitted/…) is left untouched.
                for k, v in task_defaults.items():
                    if k == 'status':
                        continue
                    if k in REQUEST_TO_TASK_COPY_FIELDS:
                        # Request context is owned by the ProdRequest row
                        # (below) and projected through the physics
                        # configuration; the task columns are creation-time
                        # seeds, independently mutable thereafter
                        # (CAMPAIGN_CONTINUUM.md — requests over physics
                        # configurations).
                        continue
                    if k == 'overrides':
                        # Merge so non-csv_import override keys (added by
                        # operators or other code paths) are preserved;
                        # the csv_import bucket itself is fully refreshed.
                        merged = dict(existing.overrides or {})
                        merged.update(v)
                        v = merged
                    setattr(existing, k, v)
                existing.save()
                summary['updated'] += 1
            else:
                ProdTask.objects.create(name=task_name, **task_defaults)
                summary['created'] += 1

            # The request record: one ProdRequest per CSV row, bound to the
            # physics configuration (never a campaign) through the composed
            # name of the edition it was recorded against — a name
            # reference, the same convention as ``replaced_by``. Editions
            # in any campaign reach it by resolving to the same
            # configuration (pc_request_projection). Triage fields and
            # status belong to the production team and are never touched
            # by re-import.
            request_fields = dict(
                requestor=(row.get('DSC or PWG') or '').strip().upper(),
                simu_path=ds_path,
                gen_config=gen_ver,
                nevents=nevents,
                background=(row.get('Background') or '').strip(),
                description=(row.get('Description') or '').strip(),
                priority=priority,
                pre_tdr_use=_possible(row.get('Pre-TDR Use')),
                early_science_use=_yesno(row.get('Early Science Use')),
                other_use=_possible(row.get('Other Use')),
                new_request=_yesno(row.get('New Request')),
                source_url=(row.get('Issue') or '').strip(),
            )
            req = ProdRequest.objects.filter(source_row=slug).first()
            if req is None:
                req = ProdRequest(source_row=slug, status='new',
                                  created_by=created_by)
                summary['requests_created'] += 1
            else:
                summary['requests_updated'] += 1
            for k, v in request_fields.items():
                setattr(req, k, v)
            req_data = dict(req.data or {})
            req_data.update({
                'filters': filters,
                'physics_config_anchor': ds.composed_name,
                'angular_range': angular_range,
                'other_use_text': (row.get('Other Use') or '').strip(),
            })
            req.data = req_data
            req.save()

    return summary


# ---------------------------------------------------------------------------
# Requests over physics configurations (CAMPAIGN_CONTINUUM.md)
# ---------------------------------------------------------------------------

def pc_anchored_requests():
    """{anchor_composed_name: [ProdRequest, ...]} for every request bound
    to a physics configuration. The anchor is the composed name of the
    edition the request was recorded against; any edition resolving to
    the same configuration carries the request."""
    by_anchor = {}
    for req in ProdRequest.objects.order_by('pk'):
        anchor = (req.data or {}).get('physics_config_anchor')
        if anchor:
            by_anchor.setdefault(anchor, []).append(req)
    return by_anchor


def pc_request_projection(datasets):
    """Project PC-anchored requests onto dataset editions.

    Returns {composed_name: [ProdRequest, ...]} for every given edition
    that resolves to the same physics configuration as a request's
    anchor — the read path behind "tasks point to requests via their
    configuration". Anchor and edition are compared by
    ``physics_config_key``; an anchor naming no surviving dataset is
    reported, never silently dropped.
    """
    from .physics_config import physics_config_key
    by_anchor = pc_anchored_requests()
    if not by_anchor or not datasets:
        return {}
    select = ('physics_tag', 'evgen_tag', 'background_tag')
    heads = (Dataset.objects.filter(composed_name__in=by_anchor)
             .select_related(*select)
             .order_by('composed_name', 'block_num', 'pk')
             .distinct('composed_name'))
    requests_by_key = {}
    resolved = set()
    for head in heads:
        key = physics_config_key(head)['key']
        resolved.add(head.composed_name)
        requests_by_key.setdefault(key, []).extend(by_anchor[head.composed_name])
    orphaned = set(by_anchor) - resolved
    if orphaned:
        _log.warning(
            'pc_request_projection: %d request anchor(s) name no dataset: %s',
            len(orphaned), sorted(orphaned)[:5])
    projection = {}
    seen = set()
    for dataset in datasets:
        name = dataset.composed_name
        if name in seen:
            continue
        seen.add(name)
        matched = requests_by_key.get(physics_config_key(dataset)['key'])
        if matched:
            projection[name] = matched
    return projection


def prodrequest_compose(*, created_by, pwg='', dsc='', description='',
                        nevents=None, process='', beam='', species='',
                        q2='', generator='', generator_version='',
                        sample='', pc_anchor='', simu_path='',
                        contact_name='', contact_email='',
                        repository='', intended_use=''):
    """Create a production request from the request composer.

    The mapping is deterministic: the composer's physics axes land in
    ``data['filters']`` (the semantic identity block requests resolve
    by), and adopting an existing configuration sets
    ``data['physics_config_anchor']`` — the same binding the CSV import
    writes, so a composed request projects onto editions exactly like
    an imported one. Production-team triage fields are left for the
    production team.
    """
    pwg = (pwg or '').strip()
    dsc = (dsc or '').strip()
    requestor = pwg or dsc
    contact_name = (contact_name or '').strip()
    contact_email = (contact_email or '').strip()
    if not contact_name or not contact_email:
        raise ServiceError('Contact name and email are required.')
    if '@' not in contact_email:
        raise ServiceError('Contact email does not look like an email address.')
    description = (description or '').strip()
    process = (process or '').strip()
    if not description and not process:
        raise ServiceError(
            'Describe the physics: pick a process or write a description.')
    if nevents not in (None, ''):
        try:
            nevents = int(nevents)
        except (TypeError, ValueError):
            raise ServiceError('Event count must be a number.')
        if nevents < 0:
            raise ServiceError('Event count must be positive.')
    else:
        nevents = None
    pc_anchor = (pc_anchor or '').strip()
    if pc_anchor and not Dataset.objects.filter(
            composed_name=pc_anchor).exists():
        raise ServiceError(
            f'Adopted configuration {pc_anchor!r} names no known dataset.')

    filters = {
        'process': process,
        'beam': (beam or '').strip(),
        'species': (species or '').strip(),
        'q2': (q2 or '').strip(),
        'generator': (generator or '').strip(),
        'sample': (sample or '').strip(),
    }
    data = {
        'composer': True,
        'filters': filters,
    }
    if pwg:
        data['pwg'] = pwg
    if dsc:
        data['dsc'] = dsc
    if pc_anchor:
        data['physics_config_anchor'] = pc_anchor
    data['contact_name'] = contact_name
    data['contact_email'] = contact_email
    if repository:
        data['repository'] = (repository or '').strip()
    if intended_use:
        data['intended_use'] = (intended_use or '').strip()
    gen_config = ' '.join(
        part for part in ((generator or '').strip(),
                          (generator_version or '').strip()) if part)

    request_row = ProdRequest.objects.create(
        requestor=requestor,
        description=description,
        nevents=nevents,
        gen_config=gen_config,
        simu_path=(simu_path or '').strip(),
        new_request=True,
        status='new',
        source_row=f'composer:{_uuid.uuid4().hex[:12]}',
        data=data,
        created_by=created_by or 'request_composer',
    )
    from monitor_app.epicprod_logging import log_epicprod_action
    log_epicprod_action(
        'web', 'prodrequest_compose',
        subject_type='prod_request', subject_key=str(request_row.pk),
        username=created_by, sublevel='normal', live_default=True,
        requestor=requestor, pc_anchor=pc_anchor or '')
    return {
        'id': request_row.pk,
        'requestor': requestor,
        'pc_anchor': pc_anchor,
        'nevents': nevents,
        'status': request_row.status,
    }


# ---------------------------------------------------------------------------
# Past-campaign output ingest (epic-prod FULL/RECO/<version>/index.md)
# ---------------------------------------------------------------------------

EPIC_PROD_PATH = '/data/wenauseic/github/epic-prod'
PAST_CAMPAIGN_STAGES = ('FULL', 'RECO')
PAST_CAMPAIGN_YEAR_PREFIXES = ('25.', '26.')   # 2025 + 2026 campaigns

_PAST_DID_RE   = _re.compile(r'^===\s*(epic:/\S+)\s*===\s*$')
_PAST_RSE_RE   = _re.compile(r'RSE:\s*(\S+)\s+Files:\s*(\d+)/(\d+)\s*\(([^)]+)\)')
_PAST_SIZE_RE  = _re.compile(r'Total Size:\s*([\d.]+)\s*([KMGTP]?B)\s*\((\d+)\s*files?\)', _re.I)
_PAST_SUMMARY_RE = _re.compile(r'^===\s*CAMPAIGN SUMMARY\s*===\s*$')

_SIZE_UNIT_BYTES = {'B': 1, 'KB': 1_000, 'MB': 1_000_000,
                    'GB': 1_000_000_000, 'TB': 1_000_000_000_000,
                    'PB': 1_000_000_000_000_000}


def _parse_size_to_bytes(value, unit):
    try:
        n = float(value)
    except (TypeError, ValueError):
        _log.warning('past_ingest: unparseable size value %r', value)
        return 0
    mult = _SIZE_UNIT_BYTES.get(unit.upper())
    if mult is None:
        _log.warning('past_ingest: unrecognised size unit %r', unit)
        return 0
    return int(round(n * mult))


def _parse_past_index(text):
    """Parse an epic-prod docs/<STAGE>/<version>/index.md file.

    Yields dicts: {did, rses: [{name, files, total, status}], file_count,
    data_size_bytes, complete}. The campaign summary block at the end is
    skipped.
    """
    block = None

    def _emit():
        nonlocal block
        if block:
            yield_block, block = block, None
            return yield_block
        return None

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line == '```':
            continue
        if _PAST_SUMMARY_RE.match(line):
            done = _emit()
            if done is not None:
                yield done
            break
        m = _PAST_DID_RE.match(line)
        if m:
            done = _emit()
            if done is not None:
                yield done
            block = {'did': m.group(1), 'rses': [], 'file_count': 0,
                     'data_size_bytes': 0, 'complete': True}
            continue
        if block is None:
            continue
        m = _PAST_RSE_RE.search(line)
        if m:
            files, total, status = int(m.group(2)), int(m.group(3)), m.group(4).strip()
            block['rses'].append({'name': m.group(1), 'files': files,
                                  'total': total, 'status': status})
            if status != 'complete':
                block['complete'] = False
            continue
        m = _PAST_SIZE_RE.search(line)
        if m:
            block['data_size_bytes'] = _parse_size_to_bytes(m.group(1), m.group(2))
            block['file_count'] = int(m.group(3))
            continue
    done = _emit()
    if done is not None:
        yield done


_PAST_BEAM_RE = _re.compile(r'(\d+x\d+)')
_PAST_Q2_RE   = _re.compile(r'(minQ2=\d+|q2_\d+(?:(?:to|_)\d+)?)')
_PAST_PHYS_TOP = ('DIS', 'SIDIS', 'DDIS', 'EXCLUSIVE', 'SINGLE', 'BACKGROUNDS')


_PAST_ENERGY_BARE_RE = _re.compile(
    r'\b(\d+(?:\.\d+)?(?:eV|keV|MeV|GeV|TeV))\b', _re.I)


def _extract_path_filters(segments):
    """Extract {beam, physics, q2, species, energy} from a list of path
    segments. Shared by the past-output DID and the current-tab CSV
    input-dataset path so both filter bars speak the same vocabulary.
    """
    seg_str = '/'.join(segments)
    beam_m = _PAST_BEAM_RE.search(seg_str)
    q2_m   = _PAST_Q2_RE.search(seg_str)
    physics = next((p for p in _PAST_PHYS_TOP if p in segments), '')
    species, energy = '', ''
    if physics == 'SINGLE':
        i = segments.index('SINGLE')
        if len(segments) > i + 1:
            species = segments[i + 1]
        if len(segments) > i + 2:
            energy = segments[i + 2]
    if not energy:
        em = _PAST_ENERGY_BARE_RE.search(seg_str)
        if em:
            energy = em.group(1)
    # Fold DIS subtype (NC / CC) directly into physics so the Physics
    # filter shows NC and CC as siblings of DIS, not a separate row.
    dis_type = next((t for t in ('NC', 'CC') if t in segments), '')
    if physics == 'DIS' and dis_type:
        physics = dis_type
    return {
        'beam':    beam_m.group(1) if beam_m else '',
        'physics': physics,
        'q2':      q2_m.group(1) if q2_m else '',
        'species': species,
        'energy':  energy,
    }


def _extract_past_filters(did):
    """Past-output filter fields: detector (from path segment 3) plus
    the shared {beam, physics, q2, species, energy}. Empty string for
    any dimension the path doesn't carry."""
    parts = did.split(':', 1)
    rest_str = (parts[1] if len(parts) == 2 else did).lstrip('/')
    segs = rest_str.split('/')
    detector = segs[2] if len(segs) > 2 else ''
    out = _extract_path_filters(segs[3:])
    out['detector'] = detector
    return out


def _extract_csv_filters(path, detector_config):
    """Current-tab filter fields from a CSV input dataset path.

    Path shape: /volatile/eic/EPIC/EVGEN/<physics>/<gen>/.../<beam>/...
    Geometry comes from the dataset's detector_config field (not the
    path); everything else from the path tail after the EVGEN prefix.
    """
    segs = (path or '').lstrip('/').split('/')
    # Drop the fixed prefix volatile/eic/EPIC/EVGEN when present, but
    # be tolerant if a different shape appears later.
    if len(segs) >= 4 and segs[:4] == ['volatile', 'eic', 'EPIC', 'EVGEN']:
        tail = segs[4:]
    else:
        tail = segs
    out = _extract_path_filters(tail)
    out['detector'] = detector_config or ''
    return out


def _decompose_past_did(did):
    """Break an epic-prod DID into the path-level fields we filter on.

    Expected form: epic:/STAGE/version/detector_config/<bg-chain>/<phys-chain>/beam/paramset
    Everything after detector_config is best-effort; what we don't recognise
    stays in metadata['past_output']['path_remainder'] for the row to render
    verbatim. NO-SILENT-FAILURES: a DID that doesn't even split into the
    leading STAGE/version/detector parts is logged.
    """
    parts = did.split(':', 1)
    rest = parts[1] if len(parts) == 2 else did
    rest = rest.lstrip('/')
    segs = rest.split('/')
    if len(segs) < 3:
        _log.warning('past_ingest: DID %r has too few segments', did)
        return {}
    out = {'stage': segs[0], 'version': segs[1], 'detector_config': segs[2]}
    if len(segs) > 3:
        out['path_remainder'] = '/'.join(segs[3:])
    return out


def _version_tuple(v):
    """'26.06.0' -> (26, 6, 0) for release comparison; non-numeric parts sort low."""
    return tuple(int(p) if p.isdigit() else -1 for p in str(v or '').split('.'))


def import_epic_prod_past_campaigns(*, epic_prod_path=EPIC_PROD_PATH,
                                    created_by='past_import'):
    """Import 2026 past-campaign output datasets from a cloned epic-prod.

    Walks ``docs/FULL/<v>/index.md`` and ``docs/RECO/<v>/index.md`` for
    every 2026 version listed in ``docs/_data/{full,reco}_content.yml``.
    The 'main' alias is excluded (it's a moving target, not a frozen
    archive).

    For each parsed dataset we get-or-create:
      - Campaign(name='{version}') — one campaign row per version, bare-named;
        both stages fold onto it, with per-stage totals under
        ``data['past_summary'][stage]``. Lifecycle is classified only on
        create (newer than current -> 'future', else 'past'); an existing
        campaign's lifecycle is never changed by ingest — lifecycle
        transitions are operator actions.
      - Dataset(did=PCS internal DID, dataset_name='past.{STAGE}.{ver}.{slug}'),
        with the epic-prod Rucio DID stored in metadata['source']['location']
        and the per-RSE breakdown in metadata['past_output'].
      - ProdTask(name='past.{STAGE}.{ver}.{slug}', status='past_output',
        linked to the Dataset, anchor ProdConfig, and Campaign), carrying the
        produced dataset as a unified ``overrides['outputs']`` entry
        (EPICPROD_DATA_LINEAGE.md schema — never the legacy ``past_output``
        block).

    Idempotency key: (STAGE, version, epic_prod_did). Re-running refreshes
    file_count / data_size / rse breakdown but leaves any operator-touched
    status / overrides intact (same rule as csv_import).
    """
    import os as _os
    import yaml as _yaml
    physics, evgen, simu, reco, cfg, _ = _ensure_csvimport_anchors()
    current_camp = Campaign.objects.filter(lifecycle='current').first()
    current_v = _version_tuple(current_camp.name) if current_camp else None

    summary = {'campaigns': 0, 'rows': 0, 'created': 0, 'updated': 0, 'errors': []}

    versions_by_stage = {}
    for stage in PAST_CAMPAIGN_STAGES:
        yml_name = f'{stage.lower()}_content.yml'
        yml_path = _os.path.join(epic_prod_path, 'docs', '_data', yml_name)
        try:
            with open(yml_path) as f:
                entries = _yaml.safe_load(f) or []
        except (OSError, _yaml.YAMLError) as e:
            summary['errors'].append(f'{stage}: cannot read {yml_name}: {e}')
            continue
        versions_by_stage[stage] = [
            e['text'] for e in entries
            if isinstance(e, dict)
            and any(e.get('text', '').startswith(p) for p in PAST_CAMPAIGN_YEAR_PREFIXES)
            and e['text'] != 'main'
        ]

    with transaction.atomic():
        for stage, versions in versions_by_stage.items():
            for version in versions:
                campaign_name = version
                index_path = _os.path.join(epic_prod_path, 'docs', stage, version, 'index.md')
                try:
                    with open(index_path) as f:
                        text = f.read()
                except OSError as e:
                    summary['errors'].append(f'{stage}/{version}: {e}')
                    continue

                # Lifecycle is classified on create only: a produced release
                # newer than the current campaign starts as 'future', anything
                # else as 'past'. An existing campaign's lifecycle is operator
                # state and is never changed by ingest.
                lc = 'future' if (current_v is not None
                                  and _version_tuple(version) > current_v) else 'past'
                campaign, _ = Campaign.objects.get_or_create(
                    name=campaign_name,
                    defaults={'lifecycle': lc,
                              'description': f'Campaign {version} (epic-prod ingest)',
                              'created_by': created_by},
                )
                summary['campaigns'] += 1

                campaign_files = 0
                campaign_bytes = 0
                for block in _parse_past_index(text):
                    summary['rows'] += 1
                    epic_did = block['did']
                    slug = _hashlib.sha1(epic_did.encode()).hexdigest()[:12]
                    pcs_name = f'past.{stage}.{version}.{slug}'
                    decomposed = _decompose_past_did(epic_did)

                    metadata = {
                        'stage': stage.lower(),
                        'source': {'kind': 'rucio_did', 'location': epic_did},
                        'past_output': {
                            'campaign_name': campaign_name,
                            'stage': stage,
                            'version': version,
                            'rses': block['rses'],
                            'complete': block['complete'],
                            'path': decomposed,
                            'filters': _extract_past_filters(epic_did),
                            'index_path': index_path,
                        },
                    }

                    # Derive the real physics from the DID path remainder and bind
                    # the matching tag, replacing the placeholder anchor (the p1006
                    # dump). Standalone backgrounds take the signal-free p6001 tag;
                    # an unparseable remainder keeps the anchor and is surfaced.
                    # Evgen and background k tags for past rows stay a separate
                    # (manual) association.
                    remainder = decomposed.get('path_remainder', '')
                    derived = derive_physics(
                        remainder, beam=metadata['past_output']['filters'].get('beam', ''))
                    row_physics_tag = physics
                    if derived is None:
                        summary['errors'].append(
                            f'{stage}/{version}: DID {epic_did!r} physics unresolved; '
                            f'left on placeholder {physics.tag_label}')
                    elif derived.get('process') in ('BEAMGAS', 'SYNRAD'):
                        row_physics_tag = _no_signal_physics_tag()
                    else:
                        row_physics_tag, _ = find_or_create_physics_tag(
                            derived, created_by=created_by)

                    pcs_did = f'group.EIC:{pcs_name}.b1'
                    ds, ds_created = Dataset.objects.get_or_create(
                        dataset_name=pcs_name, block_num=1,
                        defaults=dict(
                            scope='group.EIC', did=pcs_did,
                            detector_version=version,
                            detector_config=decomposed.get('detector_config', ''),
                            campaign=campaign,
                            physics_tag=row_physics_tag, evgen_tag=evgen,
                            simu_tag=simu, reco_tag=reco,
                            file_count=block['file_count'],
                            data_size=block['data_size_bytes'],
                            description='',
                            metadata=metadata,
                            created_by=created_by,
                        ),
                    )
                    if not ds_created:
                        ds.file_count = block['file_count']
                        ds.data_size = block['data_size_bytes']
                        ds.metadata = metadata
                        ds.campaign = campaign
                        ds.detector_version = version
                        ds.detector_config = decomposed.get('detector_config', '')
                        ds.physics_tag = row_physics_tag
                        ds.save()

                    # Unified produced-output entry (ProdTask.outputs schema);
                    # the legacy past_output block stays in Dataset.metadata
                    # only, never in task overrides.
                    outputs_entry = {
                        'did': epic_did,
                        'stage': stage,
                        'version': version,
                        'filters': metadata['past_output']['filters'],
                        'rses': [{'rse': r.get('name'),
                                  'files': r.get('files'),
                                  'total': r.get('total'),
                                  'complete': r.get('status') == 'complete'}
                                 for r in (block['rses'] or [])],
                        'file_count': block['file_count'],
                        'bytes': block['data_size_bytes'],
                        'complete': block['complete'],
                        'checked_at': _timezone.now().isoformat(),
                    }
                    task_defaults = dict(
                        description='',
                        dataset=ds,
                        prod_config=cfg,
                        campaign=campaign,
                        overrides={'outputs': [outputs_entry]},
                        created_by=created_by,
                    )
                    existing = ProdTask.objects.filter(name=pcs_name).first()
                    if existing:
                        preserve_status = existing.status != 'past_output'
                        for k, v in task_defaults.items():
                            if k == 'overrides':
                                merged = dict(existing.overrides or {})
                                merged.update(v)
                                # Re-import heals any legacy-shaped row.
                                merged.pop('past_output', None)
                                v = merged
                            setattr(existing, k, v)
                        if not preserve_status:
                            existing.status = 'past_output'
                        existing.save()
                        summary['updated'] += 1
                    else:
                        ProdTask.objects.create(name=pcs_name,
                                                status='past_output',
                                                **task_defaults)
                        summary['created'] += 1
                    campaign_files += block['file_count']
                    campaign_bytes += block['data_size_bytes']

                # Stage-keyed totals: one campaign row spans both stages, so
                # each stage pass writes only its own entry.
                data = dict(campaign.data or {})
                past_summary = data.get('past_summary')
                if not isinstance(past_summary, dict) or 'file_count' in past_summary:
                    past_summary = {}
                past_summary[stage] = {
                    'file_count': campaign_files,
                    'data_size_bytes': campaign_bytes,
                }
                data['past_summary'] = past_summary
                campaign.data = data
                campaign.save(update_fields=['data', 'updated_at'])

    return summary


# ---------------------------------------------------------------------------
# JLab Rucio current-campaign snapshot
#
# Output datasets land at JLab Rucio under scope `epic`. The nightly
# epic-prod GitHub Action that generates docs/{FULL,RECO}/<v>/index.md is
# exactly this same Rucio query, dumped to markdown. We pull it directly
# so the Current tab can show 'Output: <files / size / RSEs>' on each
# task row without waiting for the upstream nightly rebuild.
#
# Credentials are the public read-only eicread/eicread account
# (matches the DISpatcher jlab-rucio MCP config). Override via env.
# ---------------------------------------------------------------------------

JLAB_RUCIO_URL      = '/'.join(['https://rucio-server.jlab.org:443'])
JLAB_RUCIO_ACCOUNT  = 'eicread'
JLAB_RUCIO_USERNAME = 'eicread'
JLAB_RUCIO_PASSWORD = 'eicread'
RUCIO_SNAPSHOT_DIR  = _os.path.join(_settings.SWF_TMP_DIR, 'rucio-snapshots')


def _rucio_timeline_path(campaign_name, *, snapshot_dir=RUCIO_SNAPSHOT_DIR):
    return _os.path.join(snapshot_dir, f'timeline-{campaign_name}.json')


def _write_json_atomic(path, value):
    tmp = f'{path}.tmp'
    with open(tmp, 'w') as f:
        _json.dump(value, f, indent=2)
    _os.replace(tmp, path)


def _jlab_rucio_auth(timeout=30):
    """userpass-auth against JLab Rucio. Returns the X-Rucio-Auth-Token."""
    import urllib.request as _ur
    import ssl as _ssl
    import os as _os
    ctx = _ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = _ssl.CERT_NONE
    url = (_os.environ.get('JLAB_RUCIO_URL') or JLAB_RUCIO_URL) + '/auth/userpass'
    req = _ur.Request(url)
    req.add_header('X-Rucio-Account',  _os.environ.get('JLAB_RUCIO_ACCOUNT',  JLAB_RUCIO_ACCOUNT))
    req.add_header('X-Rucio-Username', _os.environ.get('JLAB_RUCIO_USERNAME', JLAB_RUCIO_USERNAME))
    req.add_header('X-Rucio-Password', _os.environ.get('JLAB_RUCIO_PASSWORD', JLAB_RUCIO_PASSWORD))
    resp = _ur.urlopen(req, context=ctx, timeout=timeout)
    token = resp.headers['X-Rucio-Auth-Token']
    if not token:
        raise ServiceError('JLab Rucio auth returned no token')
    return token


def _jlab_rucio_get(path, token, *, timeout=60, **q):
    """GET a JLab Rucio path with the auth token; returns response text."""
    import urllib.request as _ur
    import urllib.parse as _up
    import ssl as _ssl
    import os as _os
    ctx = _ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = _ssl.CERT_NONE
    url = (_os.environ.get('JLAB_RUCIO_URL') or JLAB_RUCIO_URL) + path
    if q:
        url += '?' + _up.urlencode(q)
    req = _ur.Request(url)
    req.add_header('X-Rucio-Auth-Token', token)
    return _ur.urlopen(req, context=ctx, timeout=timeout).read().decode()


def _ndjson(text):
    """Parse a newline-delimited-JSON Rucio response (strings or dicts)."""
    import json as _json
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(_json.loads(line))
        except _json.JSONDecodeError:
            out.append(line)
    return out


def fetch_jlab_rucio_campaign(campaign_path, *, scope='epic', token=None,
                              max_workers=16):
    """Fetch the full Rucio snapshot for one campaign path (e.g. '/RECO/26.02.0').

    Returns {count, datasets:[{did, length, bytes, rse_replicas:[{rse, ...}]}, ...]}.
    Each dataset's rse_replicas mirrors what /replicas/<scope>/<name>/datasets
    returns — per-RSE found/total/state/bytes, exactly the shape the
    epic-prod nightly workflow uses to produce its index.md replica lines.

    Per-dataset metadata + replica fetches run in a ThreadPoolExecutor so a
    365-dataset campaign completes in ~5-10s instead of ~80s, keeping the
    'Update from Rucio' button under Apache's request timeout.
    """
    import json as _json
    from concurrent.futures import ThreadPoolExecutor as _Pool
    if token is None:
        token = _jlab_rucio_auth()
    names = _ndjson(_jlab_rucio_get(
        f'/dids/{scope}/dids/search', token,
        type='dataset', name=campaign_path + '/*'))

    def _one(name):
        if not isinstance(name, str):
            return None
        try:
            meta = _json.loads(_jlab_rucio_get(f'/dids/{scope}/{name}', token))
        except Exception as e:                                # noqa: BLE001
            _log.warning('rucio meta %s/%s: %s', scope, name, e)
            meta = {}
        try:
            rse_records = _ndjson(
                _jlab_rucio_get(f'/replicas/{scope}/{name}/datasets', token))
        except Exception as e:                                # noqa: BLE001
            _log.warning('rucio replicas %s/%s: %s', scope, name, e)
            rse_records = []
        return {
            'did':          f'{scope}:{name}',
            'length':       meta.get('length'),
            'bytes':        meta.get('bytes'),
            'rse_replicas': rse_records,
        }

    with _Pool(max_workers=max_workers) as pool:
        results = list(pool.map(_one, names))
    datasets = [r for r in results if r is not None]
    return {'count': len(datasets), 'datasets': datasets}


def fetch_jlab_rucio_did(scope, name):
    """Live read of a single JLab Rucio DID for the self-hosted detail page.

    Pure read over the public eicread userpass — no agent credential, no cache;
    what it returns is true *now*. Generic over DID type: an input EVGEN dataset
    and an output RECO dataset render through this same path. Returns Rucio's
    full ``/meta`` (system + user attributes), the per-RSE dataset replicas, and
    a derived summary (type, total bytes, file count). The file list is fetched
    separately (``fetch_jlab_rucio_did_files``) — a populated RECO dataset has
    thousands of files. Raises ServiceError(404) for an unknown DID, (502) if
    JLab Rucio is unreachable. See docs/EPICPROD_DATA_LINEAGE.md."""
    import json as _json
    import urllib.error as _ue
    name = '/' + name.lstrip('/')   # Rucio names are leading-slashed; a proxy may collapse '//'
    try:
        token = _jlab_rucio_auth()
        meta = _json.loads(_jlab_rucio_get(f'/dids/{scope}/{name}/meta', token))
        replicas = [r for r in _ndjson(
            _jlab_rucio_get(f'/replicas/{scope}/{name}/datasets', token))
            if isinstance(r, dict)]
    except _ue.HTTPError as e:
        if e.code == 404:
            raise ServiceError(
                f'DID not found in JLab Rucio: {scope}:{name}', status=404)
        raise ServiceError(
            f'JLab Rucio error {e.code} for {scope}:{name}', status=502)
    except (_ue.URLError, OSError) as e:
        raise ServiceError(f'Could not reach JLab Rucio: {e}', status=502)
    # The DID record's length/bytes are often null; the replica rows carry the
    # real totals. Take the max across RSEs as the dataset total.
    def _from_replicas(key):
        vals = [r.get(key) for r in replicas if isinstance(r.get(key), int)]
        return max(vals) if vals else meta.get(key)
    return {
        'scope': scope, 'name': name, 'did': f'{scope}:{name}',
        'type': meta.get('did_type') or meta.get('type'),
        'account': meta.get('account'),
        'is_open': meta.get('is_open'),
        'availability': meta.get('availability'),
        'bytes': _from_replicas('bytes'),
        'file_count': _from_replicas('length'),
        'created_at': meta.get('created_at'),
        'updated_at': meta.get('updated_at'),
        'meta': meta,
        'replicas': replicas,
    }


def fetch_jlab_rucio_did_files(scope, name):
    """Live file list for a JLab Rucio DID (on-demand; can be thousands).

    Pure read; returns ``[{name, bytes, adler32, guid, events}, ...]``. Per-file
    PFN resolution is intentionally omitted here — the bulk ``/replicas`` call is
    too slow for a large dataset (it times out); the RSE-level replicas from
    ``fetch_jlab_rucio_did`` already answer 'where does it live'."""
    import urllib.error as _ue
    name = '/' + name.lstrip('/')
    try:
        token = _jlab_rucio_auth()
        files = [f for f in _ndjson(
            _jlab_rucio_get(f'/dids/{scope}/{name}/files', token, timeout=120))
            if isinstance(f, dict)]
    except _ue.HTTPError as e:
        if e.code == 404:
            raise ServiceError(
                f'DID not found in JLab Rucio: {scope}:{name}', status=404)
        raise ServiceError(
            f'JLab Rucio error {e.code} for {scope}:{name}', status=502)
    except (_ue.URLError, OSError) as e:
        raise ServiceError(f'Could not reach JLab Rucio: {e}', status=502)
    return files


def _request_input_tail(ds_path):
    """Return the comparable tail of a CSV input dataset path.

    /volatile/eic/EPIC/EVGEN/<TAIL>  ->  '<TAIL>'  (lower-cased)
    anything else                    ->  '' (no match)
    """
    if not ds_path:
        return ''
    parts = ds_path.strip('/').split('/')
    if len(parts) >= 5 and parts[:4] == ['volatile', 'eic', 'EPIC', 'EVGEN']:
        return '/'.join(parts[4:]).lower()
    return ''


def _did_path_tail(did):
    """Drop scope + /STAGE/<v>/<detector>/ and return the rest, lower-cased.

    epic:/RECO/26.02.0/epic_craterlake/DDIS/rapgap3.../noRad/ep/10x100/q2_1to10
        -> 'ddis/rapgap3.../norad/ep/10x100/q2_1to10'
    """
    name = did.split(':', 1)[-1].lstrip('/')
    parts = name.split('/')
    if len(parts) <= 3:
        return ''
    return '/'.join(parts[3:]).lower()


def _aggregate_rucio_match(matches):
    """Per-stage rollup over a list of matched dataset records.

    Rucio's /dids/<scope>/<name> endpoint returns length/bytes as null
    for these datasets; the canonical file count and byte size live on
    each RSE replica record (they all carry the same value since RSEs
    hold replicas of the same data). Take the max across RSEs.
    """
    by_stage = {}
    for m in matches:
        st = by_stage.setdefault(m['stage'], {
            'count': 0, 'files': 0, 'bytes': 0,
            'rses': {}, 'incomplete': 0,
        })
        st['count'] += 1
        ds_files = m.get('length') or 0
        ds_bytes = m.get('bytes')  or 0
        complete = True
        for r in m.get('rse_replicas', []):
            rse = r.get('rse')
            if not rse:
                continue
            ds_files = max(ds_files, r.get('length') or 0)
            ds_bytes = max(ds_bytes, r.get('bytes')  or 0)
            st['rses'].setdefault(rse, {'complete': 0, 'incomplete': 0})
            if r.get('available_length') == r.get('length') and r.get('length'):
                st['rses'][rse]['complete'] += 1
            else:
                st['rses'][rse]['incomplete'] += 1
                complete = False
        st['files'] += ds_files
        st['bytes'] += ds_bytes
        if not complete:
            st['incomplete'] += 1
    return by_stage


def _did_version(did):
    """Release version segment of a produced DID.

    ``epic:/RECO/26.04.1/epic_craterlake/...`` -> ``26.04.1`` (segment after
    the stage). Empty string if it can't be parsed.
    """
    name = (did or '').split(':', 1)[-1]
    parts = [p for p in name.split('/') if p]   # [stage, version, ...]
    return parts[1] if len(parts) > 1 else ''


def _rucio_match_to_output(m, checked_at=None):
    """Convert one produced Rucio dataset record into a unified ``outputs``
    entry (one per dataset, lifecycle-neutral) — see EPICPROD_DATA_LINEAGE.md.

    File count / byte size are taken as the max across RSE replicas (Rucio
    reports them per replica, identical across RSEs), matching
    ``_aggregate_rucio_match``.
    """
    did = m.get('did', '')
    files = m.get('length') or 0
    bytes_ = m.get('bytes') or 0
    rses = []
    complete = True
    for r in m.get('rse_replicas', []) or []:
        rse = r.get('rse')
        if not rse:
            continue
        total = r.get('length') or 0
        avail = r.get('available_length') or 0
        files = max(files, total)
        bytes_ = max(bytes_, r.get('bytes') or 0)
        rse_complete = bool(total) and avail == total
        complete = complete and rse_complete
        rses.append({'rse': rse, 'files': avail, 'total': total,
                     'complete': rse_complete})
    entry = {
        'did':        did,
        'stage':      m.get('stage', ''),
        'version':    _did_version(did),
        'filters':    _extract_past_filters(did),
        'rses':       rses,
        'file_count': files,
        'bytes':      bytes_,
        'complete':   complete if rses else False,
    }
    if checked_at:
        entry['checked_at'] = checked_at
    return entry


def _index_snapshot_by_tail(snapshot):
    """Pre-index a snapshot for fast tail-prefix lookup.

    Returns a list of (tail, dataset_record_with_stage) tuples sorted by
    tail length DESC, so the most-specific tails are tried first.
    """
    idx = []
    for cpath, info in snapshot.get('campaigns', {}).items():
        # cpath = '/RECO/26.02.0' or '/FULL/26.02.0'
        cp_parts = cpath.strip('/').split('/')
        stage = cp_parts[0] if cp_parts else ''
        for d in info.get('datasets', []):
            tail = _did_path_tail(d.get('did', ''))
            if not tail:
                continue
            idx.append((tail, {**d, 'stage': stage}))
    return idx


_Q2_MIN_RE   = _re.compile(r'^minQ2=([\d.]+)$', _re.I)
_Q2_RANGE_RE = _re.compile(r'^q2_([\d.]+)(?:to|_)([\d.]+)$', _re.I)
_Q2_POINT_RE = _re.compile(r'^q2_([\d.]+)$', _re.I)


def _q2_range(s):
    """Convert a Q² label into a (lo, hi) numeric range.

    'minQ2=1'    -> (1, inf)
    'q2_1to10'   -> (1, 10)
    'q2_20_35'   -> (20, 35)   (the underscore spelling, as in DEMP paths)
    'q2_20'      -> (20, 20)
    anything else / empty -> None
    """
    if not s:
        return None
    m = _Q2_MIN_RE.match(s)
    if m:
        return (float(m.group(1)), float('inf'))
    m = _Q2_RANGE_RE.match(s)
    if m:
        return (float(m.group(1)), float(m.group(2)))
    m = _Q2_POINT_RE.match(s)
    if m:
        v = float(m.group(1))
        return (v, v)
    return None


def _q2_overlap(req_q2, did_q2):
    """True if request and DID Q² labels could plausibly intersect."""
    if not req_q2 or not did_q2:
        return True   # absent on either side: don't block the match
    a = _q2_range(req_q2)
    b = _q2_range(did_q2)
    if a is None or b is None:
        return req_q2 == did_q2  # exact string fallback
    return a[0] <= b[1] and b[0] <= a[1]


def _filter_match(req, did):
    """True if a request's filter fields match an output DID's.

    Matches on the semantic axes shared by both schemas (detector,
    beam, physics, Q²) rather than on path strings — the output DID
    carries extra segments (generator, radiation, charge) and a
    different Q² spelling that defeat string matching. See
    project-pcs-request-vs-output-paths.
    """
    for k in ('detector', 'beam', 'physics'):
        rv, dv = req.get(k), did.get(k)
        if rv and dv and rv != dv:
            return False
        if rv and not dv:
            return False
    if not _q2_overlap(req.get('q2'), did.get('q2')):
        return False
    # Species/energy only meaningful for SINGLE-physics paths; require
    # both sides to agree when both populated.
    for k in ('species', 'energy'):
        rv, dv = req.get(k), did.get(k)
        if rv and dv and rv != dv:
            return False
    return True


def _did_tail_segments(did_tail):
    return did_tail.split('/') if did_tail else []


def _path_aligned_match(did_segs, req_segs):
    """Is `req_segs` a contiguous subsequence of `did_segs`?

    Rucio output paths often carry a background-mixing chain prefix
    (e.g. Bkg_Exact1S_2us/GoldCt/5um/) AND a Q²-bin suffix that the
    CSV input tail doesn't carry. Subsequence matching handles both.
    """
    if not req_segs or len(req_segs) > len(did_segs):
        return False
    n = len(req_segs)
    for i in range(len(did_segs) - n + 1):
        if did_segs[i:i + n] == req_segs:
            return True
    return False


def match_requests_to_rucio_snapshot(snapshot, *, campaign):
    """For every ProdTask in `campaign` whose CSV input has a usable tail,
    stash an `overrides['csv_import']['output']` rollup of matching Rucio
    datasets. A Rucio dataset matches when its DID-name tail
    (after /STAGE/<v>/<detector>/) contains the request input tail as a
    contiguous, path-aligned segment sequence — so BG-mixing prefixes
    and Q²-bin suffixes in the output don't break the match.

    Also stashes the unmatched Rucio datasets on
    ``campaign.data['rucio_unmatched']`` so the catalog view can surface
    them as synthetic table rows (unmatched output popping up).
    """
    # Precompute (rec, did_filters) so the matcher loop is O(req * did).
    idx = _index_snapshot_by_tail(snapshot)
    checked_at = snapshot.get('fetched_at')
    idx_filtered = [(rec, _extract_past_filters(rec['did']))
                    for _tail, rec in idx]
    qs = ProdTask.objects.filter(campaign=campaign).select_related('dataset')
    summary = {'tasks_seen': 0, 'tasks_matched': 0, 'tasks_unmatched': 0}
    matched_dids = set()
    for t in qs:
        summary['tasks_seen'] += 1
        # Prefer the persisted csv_import.filters block (already extracted
        # at ingest time) and fall back to a fresh extract from the CSV
        # input path for ProdTasks that predate the filter ingest.
        req_filters = ((t.overrides or {}).get('csv_import') or {}).get('filters') or {}
        if not req_filters:
            ds_path = (t.dataset.metadata or {}).get('source', {}).get('location', '') \
                if (t.dataset.metadata or {}) else ''
            req_filters = _extract_csv_filters(ds_path, t.dataset.detector_config) \
                if ds_path else {}
        matches = []
        if req_filters and any(req_filters.get(k) for k in ('beam', 'physics')):
            for rec, did_filters in idx_filtered:
                if _filter_match(req_filters, did_filters):
                    matches.append(rec)
        for m in matches:
            matched_dids.add(m['did'])
        overrides = dict(t.overrides or {})
        overrides['outputs'] = [_rucio_match_to_output(m, checked_at) for m in matches]
        # Drop the superseded aggregate if present — outputs is now the home.
        cv = overrides.get('csv_import')
        if isinstance(cv, dict) and 'output' in cv:
            cv = dict(cv)
            cv.pop('output', None)
            overrides['csv_import'] = cv
        t.overrides = overrides
        t.save(update_fields=['overrides', 'updated_at'])
        if matches:
            summary['tasks_matched'] += 1
        else:
            summary['tasks_unmatched'] += 1

    # Unmatched Rucio datasets: in the snapshot but no request matched.
    # Light-weight records — just the fields the catalog row needs.
    unmatched = []
    for cpath, info in (snapshot.get('campaigns') or {}).items():
        cp_parts = cpath.strip('/').split('/')
        stage = cp_parts[0] if cp_parts else ''
        for d in info.get('datasets') or []:
            did = d.get('did', '')
            if did and did not in matched_dids:
                # Aggregate to a single-stage rollup of the SAME shape used
                # for matched-request output so the template can reuse the
                # output-line rendering.
                rollup = _aggregate_rucio_match([{**d, 'stage': stage}])
                files = sum(s['files'] for s in rollup.values())
                bytes_ = sum(s['bytes'] for s in rollup.values())
                any_incomplete = any(s.get('incomplete', 0) for s in rollup.values())
                unmatched.append({
                    'did':        did,
                    'stage':      stage,
                    'files':      files,
                    'bytes':      bytes_,
                    'rse_names':  sorted({r.get('rse', '') for r in d.get('rse_replicas', []) if r.get('rse')}),
                    'by_stage':   rollup,
                    'incomplete': any_incomplete,
                    'filters':    _extract_past_filters(did),
                })
    campaign.data = {**(campaign.data or {}), 'rucio_unmatched': unmatched}
    campaign.save(update_fields=['data', 'updated_at'])
    summary['rucio_unmatched'] = len(unmatched)
    return summary


def migrate_outputs_schema(*, apply=False):
    """One-time migration onto the unified ``ProdTask.overrides['outputs']``
    schema — one entry per produced Rucio dataset, lifecycle-neutral
    (EPICPROD_DATA_LINEAGE.md).

    - ``past_output`` tasks: reshape ``overrides['past_output']`` + the
      Dataset's Rucio DID/counts into one ``outputs`` entry; drop the
      ``past_output`` key.
    - tasks carrying the old ``csv_import.output`` rollup: drop that aggregate
      (explicit DIDs are re-populated by the next Rucio match).

    ``apply=False`` (default) is a dry run — counts only, no writes. Per-task
    errors are collected, never swallowed.
    """
    summary = {'seen': 0, 'past_migrated': 0, 'aggregate_dropped': 0,
               'errors': []}
    for t in ProdTask.objects.select_related('dataset').iterator():
        try:
            ov = dict(t.overrides or {})
            changed = False
            po = ov.get('past_output')
            if po and not ov.get('outputs'):
                md = (t.dataset.metadata or {}) if t.dataset else {}
                did = (md.get('source') or {}).get('location', '') if md else ''
                rses = [{'rse': r.get('name'), 'files': r.get('files'),
                         'total': r.get('total'),
                         'complete': r.get('status') == 'complete'}
                        for r in (po.get('rses') or [])]
                ov['outputs'] = [{
                    'did':        did,
                    'stage':      po.get('stage', ''),
                    'version':    po.get('version', ''),
                    'filters':    po.get('filters', {}),
                    'rses':       rses,
                    'file_count': t.dataset.file_count if t.dataset else 0,
                    'bytes':      t.dataset.data_size if t.dataset else 0,
                    'complete':   po.get('complete', True),
                }]
                summary['past_migrated'] += 1
                changed = True
            if 'past_output' in ov:
                ov.pop('past_output', None)
                changed = True
            cv = ov.get('csv_import')
            if isinstance(cv, dict) and 'output' in cv:
                cv = dict(cv)
                cv.pop('output', None)
                ov['csv_import'] = cv
                summary['aggregate_dropped'] += 1
                changed = True
            summary['seen'] += 1
            if changed and apply:
                t.overrides = ov
                t.save(update_fields=['overrides', 'updated_at'])
        except Exception as e:                                # noqa: BLE001
            summary['errors'].append(f'{t.name}: {e}')
    return summary


PROPAGATION_STATES = ('continue', 'hold', 'final')


def dataset_propagation_set(composed_names, state, comment, *, replaced_by='',
                            clear_replaced_by=False, changed_by='',
                            filter_state='', origin=None):
    """Set the cross-campaign propagation disposition on dataset editions.

    One call = one operator action, single or bulk. Every named composed
    identity gets the state (and the optional ``replaced_by`` successor
    reference), with one append-only history entry recorded on the
    identity's head row — deterministically the first by (block, pk), since
    past-campaign identities carry several physical rows on one block —
    under ``metadata['propagation']['history']``;
    state, previous, comment (required: the why and its source travel
    together), changed_by, changed_at. The ``propagation`` column mirrors
    the newest entry on every block row.

    ``replaced_by`` semantics: an empty value leaves the successor
    reference untouched; ``clear_replaced_by=True`` blanks it explicitly
    (the two are mutually exclusive). Whenever the reference is touched,
    the history entry records ``replaced_by`` (the new value, '' on a
    clear) and — when it changed — ``previous_replaced_by``, so every flip
    is unwindable from its own history entry.

    Exactly one ``dataset_propagation_set`` action-stream event is logged
    per call, carrying the changed count, the comment, and the selecting
    filter — never one event per dataset. The comment is remembered as the
    caller's next default (UserPreference ``propagation_last_comment``).

    Unknown names and no-op flips are counted and returned, never silently
    dropped. Raises ServiceError on an invalid state or an empty comment.

    ``origin`` marks an AI-proposed change approved by a human
    (AI_PROPOSALS.md): a dict with ``proposer``, ``scan_version``,
    ``batch_id``, ``proposed_at``. It is stamped on both the history entry
    and the action-stream event (``origin: ai_proposal``), so AI-originated
    mutations stay distinguishable from purely human ones.
    """
    from monitor_app.epicprod_logging import log_epicprod_action
    from monitor_app.models import UserPreference

    state = (state or '').strip()
    comment = (comment or '').strip()
    replaced_by = (replaced_by or '').strip()
    names = [n.strip() for n in (composed_names or []) if n and n.strip()]
    if state not in PROPAGATION_STATES:
        raise ServiceError(
            f'propagation state must be one of '
            f'{", ".join(PROPAGATION_STATES)}; got {state!r}')
    if not comment:
        raise ServiceError('comment is required on every propagation change')
    if not names:
        raise ServiceError('no dataset names supplied')
    if replaced_by and clear_replaced_by:
        raise ServiceError(
            'replaced_by and clear_replaced_by are mutually exclusive')

    changed, unchanged, unknown = [], [], []
    now = _timezone.now().isoformat()
    with transaction.atomic():
        for name in names:
            rows = list(Dataset.objects
                        .filter(composed_name=name)
                        .order_by('block_num', 'pk'))
            if not rows:
                unknown.append(name)
                continue
            head = rows[0]
            if (head.propagation == state
                    and (not replaced_by or head.replaced_by == replaced_by)
                    and not (clear_replaced_by and head.replaced_by)):
                unchanged.append(name)
                continue
            entry = {
                'state': state,
                'previous': head.propagation,
                'comment': comment,
                'changed_by': changed_by or '',
                'changed_at': now,
            }
            if replaced_by or clear_replaced_by:
                entry['replaced_by'] = replaced_by
                if head.replaced_by != replaced_by:
                    entry['previous_replaced_by'] = head.replaced_by
            if origin:
                entry['origin'] = str(origin.get('kind') or 'ai_proposal')
                if origin.get('proposer'):
                    entry['proposer'] = str(origin.get('proposer'))
                if origin.get('undo_of'):
                    entry['undo_of'] = origin['undo_of']
            metadata = dict(head.metadata or {})
            propagation_block = dict(metadata.get('propagation') or {})
            history = list(propagation_block.get('history') or [])
            history.append(entry)
            propagation_block['history'] = history
            metadata['propagation'] = propagation_block
            head.metadata = metadata
            for row in rows:
                row.propagation = state
                if replaced_by:
                    row.replaced_by = replaced_by
                elif clear_replaced_by:
                    row.replaced_by = ''
            head.save(update_fields=['propagation', 'replaced_by', 'metadata'])
            for row in rows[1:]:
                row.save(update_fields=['propagation', 'replaced_by'])
            changed.append(name)

    if changed_by:
        UserPreference.set_pref(changed_by, 'propagation_last_comment', comment)

    extra = {'changed': len(changed), 'unchanged': len(unchanged),
             'unknown': len(unknown), 'state': state, 'comment': comment}
    if replaced_by:
        extra['replaced_by'] = replaced_by
    if clear_replaced_by:
        extra['replaced_by_cleared'] = True
    if filter_state:
        extra['filter'] = filter_state
    if origin:
        extra['origin'] = str(origin.get('kind') or 'ai_proposal')
        if origin.get('proposer'):
            extra['proposer'] = str(origin['proposer'])
        if origin.get('scan_version') is not None:
            extra['proposal_scan_version'] = origin.get('scan_version')
        if origin.get('batch_id'):
            extra['proposal_batch_id'] = str(origin['batch_id'])
        if origin.get('proposed_at'):
            extra['proposed_at'] = str(origin['proposed_at'])
        if origin.get('undo_of'):
            extra['undo_of'] = origin['undo_of']
    log_id = log_epicprod_action(
        'web', 'dataset_propagation_set',
        subject_type='dataset' if len(changed) == 1 else '',
        subject_key=changed[0] if len(changed) == 1 else '',
        username=changed_by,
        sublevel='normal', live_default=True,
        message=(f'propagation -> {state} on {len(changed)} dataset(s): '
                 f'{comment}'),
        **extra,
    )
    return {'changed': changed, 'unchanged': unchanged, 'unknown': unknown,
            'state': state, 'log_id': log_id}


def summarize_rucio_timeline(snapshot, *, bin_hours=12):
    """Build a per-bin cumulative arrival timeline from a Rucio snapshot.

    'Arrival' = the earliest created_at across all RSE replicas of a
    dataset. Datasets without any usable timestamp are dropped. Each
    arrival lands in the nearest `bin_hours`-wide bin (default 12h,
    aligned to UTC midnight). Returns a dict suitable for Plotly:

        {'dates': ['YYYY-MM-DDTHH:00:00', ...],
         'bin_hours': 12,
         'simu': {'cum_datasets':[...], 'cum_files':[...], 'cum_bytes':[...]},
         'reco': {'cum_datasets':[...], 'cum_files':[...], 'cum_bytes':[...]}}
    """
    from email.utils import parsedate_to_datetime as _pd
    import datetime as _dt
    bin_size = _dt.timedelta(hours=bin_hours)

    def _bucket(dt):
        """Floor `dt` to the nearest `bin_hours`-bin aligned to UTC midnight."""
        midnight = dt.replace(hour=0, minute=0, second=0, microsecond=0)
        offset = (dt - midnight).total_seconds()
        return midnight + _dt.timedelta(
            seconds=(int(offset) // int(bin_size.total_seconds()))
                    * int(bin_size.total_seconds()))

    arrivals = []  # (bucket_iso, stage, files, bytes)
    for cpath, info in (snapshot.get('campaigns') or {}).items():
        cp_parts = cpath.strip('/').split('/')
        stage = cp_parts[0] if cp_parts else ''
        for d in info.get('datasets') or []:
            rses = d.get('rse_replicas') or []
            tsps = []
            files = bytes_ = 0
            for r in rses:
                ts_str = r.get('created_at')
                if ts_str:
                    try:
                        tsps.append(_pd(ts_str))
                    except Exception:                         # noqa: BLE001
                        pass
                files = max(files, r.get('length') or 0)
                bytes_ = max(bytes_, r.get('bytes')  or 0)
            if not tsps:
                continue
            arrivals.append((_bucket(min(tsps)).strftime('%Y-%m-%dT%H:%M:%S'),
                             stage, files, bytes_))
    arrivals.sort()
    if not arrivals:
        return {'dates': [], 'bin_hours': bin_hours, 'simu': {}, 'reco': {}}

    first = _dt.datetime.fromisoformat(arrivals[0][0])
    last_arr = _dt.datetime.fromisoformat(arrivals[-1][0])
    # Extend the axis to the current bin (UTC) so a quiet stretch
    # between the last arrival and now shows as a flat segment.
    # Don't go past 'now' — no empty future bins on the axis.
    last = max(last_arr, _bucket(_dt.datetime.utcnow()))
    span = last - first
    n_bins = int(span.total_seconds() // bin_size.total_seconds()) + 1
    dates = [(first + i * bin_size).strftime('%Y-%m-%dT%H:%M:%S')
             for i in range(n_bins)]
    idx = {d: i for i, d in enumerate(dates)}

    def _empty():
        return {'cum_datasets': [0]*n_bins, 'cum_files': [0]*n_bins, 'cum_bytes': [0]*n_bins}
    out = {'dates': dates, 'bin_hours': bin_hours, 'simu': _empty(), 'reco': _empty()}
    per_bin = {'FULL': _empty(), 'RECO': _empty()}
    for d, stage, files, bytes_ in arrivals:
        if stage not in per_bin or d not in idx:
            continue
        i = idx[d]
        per_bin[stage]['cum_datasets'][i] += 1
        per_bin[stage]['cum_files'][i]    += files
        per_bin[stage]['cum_bytes'][i]    += bytes_
    for stage, key in (('FULL', 'simu'), ('RECO', 'reco')):
        cd = cf = cb = 0
        for i in range(n_bins):
            cd += per_bin[stage]['cum_datasets'][i]
            cf += per_bin[stage]['cum_files'][i]
            cb += per_bin[stage]['cum_bytes'][i]
            out[key]['cum_datasets'][i] = cd
            out[key]['cum_files'][i]    = cf
            out[key]['cum_bytes'][i]    = cb
    return out


def _detect_active_releases(token=None, *, year_prefix='26.'):
    """Per-release dataset counts under epic:/RECO/<v>/ in JLab Rucio.

    Returns list of {version, count} for every <year_prefix>x.y release
    that has at least one dataset, sorted newest-first by component
    version. 'main' excluded. Trial runs that land a handful of
    datasets before real production starts are deliberately NOT
    filtered out here — the operator judges from the counts, and
    nothing is auto-promoted. Humans switch (see
    feedback-humans-switch-lifecycle).
    """
    if token is None:
        token = _jlab_rucio_auth()
    names = _ndjson(_jlab_rucio_get(
        '/dids/epic/dids/search', token, type='dataset', name='/RECO/*'))
    from collections import Counter as _Counter
    counts = _Counter()
    for n in names:
        if not isinstance(n, str):
            continue
        parts = n.lstrip('/').split('/')
        if len(parts) < 2:
            continue
        v = parts[1]
        if v == 'main' or not v.startswith(year_prefix):
            continue
        counts[v] += 1
    def _key(v):
        out = []
        for part in v.split('.'):
            try:
                out.append((0, int(part)))
            except ValueError:
                out.append((1, part))
        return tuple(out)
    return [{'version': v, 'count': counts[v]}
            for v in sorted(counts, key=_key, reverse=True)]


def load_rucio_snapshot(campaign_name, *, snapshot_dir=RUCIO_SNAPSHOT_DIR):
    """Read a saved JLab Rucio snapshot. Returns None if absent."""
    import json as _json
    import os as _os
    path = _os.path.join(snapshot_dir, f'current-{campaign_name}.json')
    if not _os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return _json.load(f)
    except (OSError, _json.JSONDecodeError) as e:
        _log.warning('load_rucio_snapshot %s: %s', path, e)
        return None


def load_rucio_timeline(campaign_name, *, snapshot_dir=RUCIO_SNAPSHOT_DIR):
    """Read the precomputed Rucio arrivals timeline. Returns None if absent."""
    path = _rucio_timeline_path(campaign_name, snapshot_dir=snapshot_dir)
    if not _os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return _json.load(f)
    except (OSError, _json.JSONDecodeError) as e:
        _log.warning('load_rucio_timeline %s: %s', path, e)
        return None


def import_jlab_rucio_current_snapshot(*, campaign_name=None,
                                      snapshot_dir=RUCIO_SNAPSHOT_DIR,
                                      created_by='rucio_snapshot'):
    """Pull the JLab Rucio snapshot for the PCS current campaign and save
    it under the snapshot directory.

    campaign_name: '26.02.0' (or override). If None, uses the
        lifecycle='current' Campaign. Both /RECO/<v> and /FULL/<v> are
        fetched.
    snapshot_dir: writable directory. One JSON file per current campaign:
        '<snapshot_dir>/current-<campaign>.json'.
    NO-SILENT-FAILURES: every network / parse error is collected into
    summary['errors'] and surfaced to the operator.
    """
    import json as _json
    import os as _os
    import time as _time

    summary = {'campaign': '', 'paths': {}, 'errors': [], 'snapshot_path': ''}

    # If no name given, default to lifecycle='current'. Callers may pass
    # a 'last' campaign name explicitly.
    if campaign_name is None:
        current = (Campaign.objects.filter(lifecycle='current')
                   .order_by('-updated_at').first())
        if not current:
            raise ServiceError('No current Campaign defined in PCS')
        campaign_name = current.name
    summary['campaign'] = campaign_name

    _os.makedirs(snapshot_dir, exist_ok=True)
    out_path = _os.path.join(snapshot_dir, f'current-{campaign_name}.json')
    summary['snapshot_path'] = out_path

    try:
        token = _jlab_rucio_auth()
    except Exception as e:                                    # noqa: BLE001
        raise ServiceError(f'JLab Rucio auth failed: {e}')

    snapshot = {
        'fetched_at':    _time.strftime('%Y-%m-%dT%H:%M:%S%z'),
        'scope':         'epic',
        'campaign_name': campaign_name,
        'campaigns':     {},
    }
    for stage in ('RECO', 'FULL'):
        cpath = f'/{stage}/{campaign_name}'
        try:
            snapshot['campaigns'][cpath] = fetch_jlab_rucio_campaign(
                cpath, token=token)
            summary['paths'][cpath] = snapshot['campaigns'][cpath]['count']
        except Exception as e:                                # noqa: BLE001
            summary['errors'].append(f'{cpath}: {e}')
            snapshot['campaigns'][cpath] = {'count': 0, 'datasets': [],
                                            'error': str(e)}
            summary['paths'][cpath] = 0

    _write_json_atomic(out_path, snapshot)
    summary['file_bytes'] = _os.path.getsize(out_path)
    try:
        timeline = summarize_rucio_timeline(snapshot)
        timeline['campaign_name'] = campaign_name
        timeline['snapshot_fetched_at'] = snapshot['fetched_at']
        timeline_path = _rucio_timeline_path(campaign_name, snapshot_dir=snapshot_dir)
        _write_json_atomic(timeline_path, timeline)
        summary['timeline_path'] = timeline_path
        summary['timeline_bytes'] = _os.path.getsize(timeline_path)
    except Exception as e:                                    # noqa: BLE001
        summary['errors'].append(f'timeline summary failed: {e}')

    # Detect active 26.x releases in JLab Rucio + stash on the PCS
    # current Campaign so the catalog can surface a 'Switch current'
    # banner. AI proposes; human switches (feedback-humans-switch-
    # lifecycle).
    try:
        token = _jlab_rucio_auth()
        detected = _detect_active_releases(token=token)
    except Exception as e:                                    # noqa: BLE001
        summary['errors'].append(f'detect_active_releases: {e}')
        detected = []
    summary['detected_releases'] = detected

    # Cache the per-request Output rollup on each ProdTask in this
    # campaign so the Current tab can render it without re-reading
    # the snapshot file on every page load.
    try:
        camp = Campaign.objects.get(name=campaign_name)
        # Stash the detected-releases list on the Campaign for the
        # banner; rename / promotion is a separate operator action.
        camp.data = {**(camp.data or {}), 'detected_releases': detected}
        camp.save(update_fields=['data', 'updated_at'])
        # Match the request set to the Rucio snapshot only for the
        # 'current' campaign — the 'last' campaign has no linked
        # request rows (CSV requests are bound to current), so there's
        # nothing to roll up.
        if camp.lifecycle == 'current':
            match_summary = match_requests_to_rucio_snapshot(snapshot, campaign=camp)
            summary['match'] = match_summary
    except Campaign.DoesNotExist:
        summary['errors'].append(
            f"campaign '{campaign_name}' not in PCS - skipping match step")
    except Exception as e:                                    # noqa: BLE001
        summary['errors'].append(f'request match step failed: {e}')

    return summary


def refresh_rucio_snapshots(*, created_by='rucio_snapshot'):
    """Pull the JLab Rucio snapshot for the current (+last) PCS campaign(s) and
    rematch produced datasets onto each task's ``overrides['outputs']``.

    The work behind the catalog's "Update from Rucio" button. Slow and
    network-bound (a live JLab Rucio fetch of /RECO + /FULL plus the match over
    every task), so it runs off the web request — in the prod-ops agent's
    ``rucio_snapshot_update`` doer. Producing campaigns (fresh arrivals,
    whatever their lifecycle slot) are targeted alongside current and
    last — the producing tab's Rucio timeline comes from here. Returns
    ``{'summaries': [...], 'errors': [...]}``; errors are collected per
    campaign, never swallowed.
    """
    from pcs.views import _campaigns_with_inflow

    targets = list(Campaign.objects.filter(lifecycle__in=['current', 'last'])
                   .order_by('lifecycle'))
    seen = {camp.pk for camp in targets}
    targets += [camp for camp, _ in _campaigns_with_inflow()
                if camp.pk not in seen]
    out = {'summaries': [], 'errors': []}
    if not targets:
        out['errors'].append('No current Campaign defined in PCS')
        return out
    for camp in targets:
        try:
            out['summaries'].append(import_jlab_rucio_current_snapshot(
                campaign_name=camp.name, created_by=created_by))
        except Exception as e:                                # noqa: BLE001
            out['errors'].append(f'{camp.lifecycle} {camp.name}: {e}')
            continue
        if camp.lifecycle not in ('current', 'last'):
            # Producing campaigns reconcile firsthand from the snapshot
            # just fetched (CAMPAIGN_CONTINUUM.md; pcs/reconcile.py) —
            # current attaches outputs through the request match, last
            # keeps its CSV heritage, so neither is reconciled here.
            try:
                from .reconcile import reconcile_campaign_from_rucio
                out['summaries'].append(
                    reconcile_campaign_from_rucio(
                        camp.name, created_by=created_by))
            except Exception as e:                            # noqa: BLE001
                out['errors'].append(f'reconcile {camp.name}: {e}')
    return out


def sweep_rucio_arrivals(*, roots=('/RECO', '/SIMU'), scope='epic',
                         window_hours=None, created_by='', instance='web'):
    """Clockwork detection of new files landing in JLab Rucio — the
    arrivals sweep (EPICPROD_DATA_LINEAGE.md).

    One ``created_after`` DID query per root (the server-side filter the
    eic/firehose action uses) across ALL campaign versions, so arrivals
    into any campaign surface whatever its lifecycle slot — the mechanism
    behind the derived 'producing' status. New files are grouped by
    campaign (the path segment under the root) and by location (the
    file's parent directory); each arriving campaign's row records the
    block ``campaign.data['arrivals']`` — last_arrival_at, window, file
    counts by root, full location breakdown. Campaign names with no
    catalog row are returned and logged, never dropped: an unknown
    arrival is the first signal of a new campaign appearing.

    The window starts at the previous sweep's timestamp (PersistentState
    ``rucio_arrivals_last_swept``), so a missed run is covered by the
    next one; ``window_hours`` overrides for hand runs. One live
    ``rucio_arrivals`` action-stream event when anything arrived; a
    quiet sweep records its bookkeeping and emits nothing.
    """
    import datetime as _dt

    from monitor_app.epicprod_logging import log_epicprod_action
    from monitor_app.models import PersistentState

    now = _timezone.now()
    if window_hours:
        start_dt = now - _dt.timedelta(hours=float(window_hours))
    else:
        last = PersistentState.get_state().get('rucio_arrivals_last_swept')
        try:
            start_dt = _dt.datetime.fromisoformat(last) if last else None
        except (TypeError, ValueError):
            start_dt = None
        if start_dt is None:
            start_dt = now - _dt.timedelta(hours=24)
    # created_after is the Rucio filter (UTC, naive by API convention);
    # window_start is the stored/displayed form — zone-aware, so every
    # surface renders it Eastern like the rest of the system.
    created_after = (start_dt.astimezone(_dt.timezone.utc)
                     .strftime('%Y-%m-%dT%H:%M:%S'))
    window_start = start_dt.isoformat()

    token = _jlab_rucio_auth()
    per_campaign = {}
    total = 0
    for root in roots:
        names = _ndjson(_jlab_rucio_get(
            f'/dids/{scope}/dids/search', token,
            type='file', created_after=created_after, name=root + '/*'))
        root_key = root.strip('/')
        for name in names:
            if not isinstance(name, str):
                continue
            segs = name.split('/')
            if len(segs) < 4 or not segs[2]:
                continue
            campaign_name, location = segs[2], '/'.join(segs[2:-1])
            camp = per_campaign.setdefault(
                campaign_name, {'files': 0, 'by_root': {}, 'locations': {}})
            camp['files'] += 1
            camp['by_root'][root_key] = camp['by_root'].get(root_key, 0) + 1
            camp['locations'][location] = camp['locations'].get(location, 0) + 1
            total += 1

    known, unknown = [], []
    with transaction.atomic():
        for campaign_name, info in sorted(per_campaign.items()):
            camp = Campaign.objects.filter(name=campaign_name).first()
            if camp is None:
                unknown.append(campaign_name)
                continue
            camp.data = {**(camp.data or {}), 'arrivals': {
                'last_arrival_at': now.isoformat(),
                'window_start': window_start,
                'files': info['files'],
                'by_root': info['by_root'],
                'locations': info['locations'],
            }}
            camp.save(update_fields=['data', 'updated_at'])
            known.append(campaign_name)
    PersistentState.update_state({'rucio_arrivals_last_swept': now.isoformat()})

    log_id = None
    if total:
        lines = []
        for campaign_name, info in sorted(per_campaign.items()):
            for location, count in sorted(info['locations'].items()):
                lines.append(f'{count:>7} {location}')
        shown = lines[:20]
        more = len(lines) - len(shown)
        message = (
            f'{total} new file(s) in JLab Rucio since '
            f'{_timezone.localtime(start_dt).strftime("%Y-%m-%d %H:%M %Z")} '
            f'[{", ".join(sorted(per_campaign))}]:\n' + '\n'.join(shown)
            + (f'\n… and {more} more location(s)' if more > 0 else ''))
        extra = {
            'total_files': total,
            'campaigns': {c: i['files'] for c, i in per_campaign.items()},
            'window_start': window_start,
            'roots': list(roots),
        }
        if unknown:
            extra['unknown_campaigns'] = unknown
        log_id = log_epicprod_action(
            instance, 'rucio_arrivals',
            username=created_by,
            sublevel='normal', live_default=True,
            message=message, **extra)
    return {'total_files': total,
            'campaigns': {c: i['files'] for c, i in per_campaign.items()},
            'known': known, 'unknown': unknown,
            'window_start': window_start, 'log_id': log_id}


# ---------------------------------------------------------------------------
# EVGEN input assimilation (JLab Rucio epic:/EVGEN/* -> PCS evgen Datasets)
# ---------------------------------------------------------------------------

EVGEN_RUCIO_SNAPSHOT_NAME = 'evgen-rucio.json'


def _extract_evgen_did_filters(did):
    """Filter axes of a JLab Rucio EVGEN DID.

    EVGEN DIDs are detector-independent and shaped
    ``epic:/EVGEN/<physics>/<generator>/<current>/<radiation>/<charge>/<beam>/<q2>``
    — no version or detector segment (unlike RECO/FULL). Strip the leading
    EVGEN and read the shared {beam, physics, q2, species, energy} from the
    rest; detector is left empty (the same EVGEN feeds any detector config).
    """
    parts = did.split(':', 1)
    rest = (parts[1] if len(parts) == 2 else did).lstrip('/')
    segs = [s for s in rest.split('/') if s]
    if segs and segs[0] == 'EVGEN':
        segs = segs[1:]
    out = _extract_path_filters(segs)
    out['detector'] = ''
    return out


def _evgen_did_tail(did):
    """Lower-cased path tail of an EVGEN DID, after scope and leading EVGEN.

    ``epic:/EVGEN/DIS/pythia8.316-1.0/NC/noRad/ep/10x100/q2_1to10``
    -> ``dis/pythia8.316-1.0/nc/norad/ep/10x100/q2_1to10``
    """
    name = did.split(':', 1)[-1].lstrip('/')
    segs = [s for s in name.split('/') if s]
    if segs and segs[0].upper() == 'EVGEN':
        segs = segs[1:]
    return '/'.join(segs).lower()


_EVGEN_Q2_MIN_RE   = _re.compile(r'^minq2=([\d.]+)$')
_EVGEN_Q2_RANGE_RE = _re.compile(r'^q2_([\d.]+)(?:to|_)([\d.]+)$')
_EVGEN_Q2_POINT_RE = _re.compile(r'^q2_([\d.]+)$')


def _looks_evgen_q2(tok):
    return tok.startswith('q2_') or tok.startswith('minq2=')


def _evgen_q2_bounds(tok):
    """Parse a lower-cased Q² token into (lo, hi, kind).

    'minq2=1'  -> (1, inf, 'min'); 'q2_1to10' -> (1, 10, 'range')  (Rucio/SIDIS);
    'q2_10_20' -> (10, 20, 'range') (EXCLUSIVE); 'q2_20' -> (20, 20, 'range').
    """
    m = _EVGEN_Q2_MIN_RE.match(tok)
    if m:
        return (float(m.group(1)), float('inf'), 'min')
    m = _EVGEN_Q2_RANGE_RE.match(tok)
    if m:
        return (float(m.group(1)), float(m.group(2)), 'range')
    m = _EVGEN_Q2_POINT_RE.match(tok)
    if m:
        v = float(m.group(1))
        return (v, v, 'range')
    return None


def _evgen_q2_match(req_tok, did_tok):
    """Q² compatibility for input resolution — distinct from the output
    sweep's ``_q2_overlap``: an explicit request range must equal the Rucio
    range exactly; a ``minQ2=N`` request matches any Rucio range lying entirely
    at or above the floor (lo >= N). No boundary-touching overlap.
    """
    rb = _evgen_q2_bounds(req_tok)
    db = _evgen_q2_bounds(did_tok)
    if rb is None or db is None:
        return req_tok == did_tok
    if rb[2] == 'min':
        return db[0] >= rb[0]
    return rb[0] == db[0] and rb[1] == db[1]


def _evgen_input_match(req_tail, did_tail):
    """True if a request EVGEN path tail resolves to a Rucio EVGEN DID tail.

    Input-side matcher — deliberately separate from the output sweep's
    ``_filter_match`` so input and output match policies evolve independently
    (a change here cannot affect the produced-output match). Both tails live in
    the same EVGEN namespace, so the request tokens must appear, in order, as a
    subsequence of the DID tokens, compared exactly EXCEPT the Q² token (range
    semantics via ``_evgen_q2_match``). Exact comparison makes the match safe by
    construction: a request that carries charge / generator / version (e.g.
    ``ep_noradcor``, ``pythia6-eic``, ``1.2.0``) only matches a DID carrying the
    same — never ep->en or a different version. A request that omits an axis
    (abstract DIS: no generator/charge) fans out to the specific Rucio datasets
    that carry it.
    """
    req = [t for t in req_tail.split('/') if t]
    did = [t for t in did_tail.split('/') if t]
    if not req:
        return False
    di = 0
    for rt in req:
        matched = False
        while di < len(did):
            dt = did[di]
            di += 1
            if _looks_evgen_q2(rt) and _looks_evgen_q2(dt):
                if _evgen_q2_match(rt, dt):
                    matched = True
                    break
            elif rt == dt:
                matched = True
                break
        if not matched:
            return False
    return True


def _rucio_evgen_entry(m, checked_at=None):
    """One resolved Rucio EVGEN dataset -> a metadata['rucio'] match entry.

    Input-side analog of ``_rucio_match_to_output``: same shape (did, rses,
    file_count, bytes, complete), but the filter axes come from the EVGEN DID
    extractor (detector-independent, no version segment). File count / byte
    size are the max across RSE replicas (Rucio reports them per replica).
    """
    did = m.get('did', '')
    files = m.get('length') or 0
    bytes_ = m.get('bytes') or 0
    rses = []
    complete = True
    for r in m.get('rse_replicas', []) or []:
        rse = r.get('rse')
        if not rse:
            continue
        total = r.get('length') or 0
        avail = r.get('available_length') or 0
        files = max(files, total)
        bytes_ = max(bytes_, r.get('bytes') or 0)
        rse_complete = bool(total) and avail == total
        complete = complete and rse_complete
        rses.append({'rse': rse, 'files': avail, 'total': total,
                     'complete': rse_complete})
    entry = {
        'did':        did,
        'stage':      'EVGEN',
        'filters':    _extract_evgen_did_filters(did),
        'rses':       rses,
        'file_count': files,
        'bytes':      bytes_,
        'complete':   complete if rses else False,
    }
    if checked_at:
        entry['checked_at'] = checked_at
    return entry


def fetch_jlab_rucio_evgen(*, scope='epic', token=None):
    """JLab Rucio EVGEN dataset inventory (``epic:/EVGEN/*``).

    Generator-level inputs are detector-independent and not campaign-
    versioned, so the whole /EVGEN tree is one inventory. Same record shape
    as ``fetch_jlab_rucio_campaign``.
    """
    return fetch_jlab_rucio_campaign('/EVGEN', scope=scope, token=token)


def refresh_evgen_rucio(*, apply=False, snapshot_dir=RUCIO_SNAPSHOT_DIR,
                        created_by='evgen_rucio'):
    """Assimilate the JLab Rucio EVGEN inventory and resolve PCS evgen
    Datasets to it. Re-sweepable: re-running picks up a grown listing.

    Fetches ``epic:/EVGEN/*`` once, saves the snapshot, and for each PCS
    Dataset with stage=evgen resolves the Rucio EVGEN dataset(s) that realize
    it — matching on the shared filter axes (beam, physics, Q² overlap), never
    on path strings: request paths carry ``minQ2=N`` and no
    generator/radiation/charge, Rucio DIDs carry the full form and a
    ``q2_<lo>to<hi>`` range, so one abstract request can resolve to several
    Q²-range datasets. The resolved refs are written to each Dataset's
    ``metadata['rucio']``; re-running overwrites them.

    ``apply=False`` (default) is a dry run — fetch + match + summary, no DB
    writes. NO-SILENT-FAILURES: network / parse / per-dataset errors are
    collected into ``summary['errors']``.
    """
    import json as _json
    import os as _os
    import time as _time

    summary = {'rucio_evgen': 0, 'datasets_seen': 0, 'datasets_matched': 0,
               'datasets_unmatched': 0, 'rucio_unmatched': 0,
               'applied': apply, 'snapshot_path': '', 'errors': [],
               'samples': []}
    try:
        token = _jlab_rucio_auth()
    except Exception as e:                                     # noqa: BLE001
        raise ServiceError(f'JLab Rucio auth failed: {e}')
    try:
        inv = fetch_jlab_rucio_evgen(token=token)
    except Exception as e:                                     # noqa: BLE001
        raise ServiceError(f'JLab Rucio EVGEN fetch failed: {e}')

    checked_at = _time.strftime('%Y-%m-%dT%H:%M:%S%z')
    # Drop obvious throwaway DIDs (a stray '.../q2_1to10/test', any case).
    records = [d for d in inv.get('datasets', [])
               if d.get('did') and not d['did'].rstrip('/').lower().endswith('/test')]
    summary['rucio_evgen'] = len(records)
    indexed = [(d, _evgen_did_tail(d['did'])) for d in records]

    snapshot = {'fetched_at': checked_at, 'scope': 'epic',
                'count': len(records), 'datasets': records}
    _os.makedirs(snapshot_dir, exist_ok=True)
    out_path = _os.path.join(snapshot_dir, EVGEN_RUCIO_SNAPSHOT_NAME)
    summary['snapshot_path'] = out_path
    try:
        with open(out_path, 'w') as f:
            _json.dump(snapshot, f, indent=2)
    except OSError as e:
        summary['errors'].append(f'snapshot write {out_path}: {e}')

    matched_dids = set()
    qs = Dataset.objects.filter(metadata__stage='evgen')
    for ds in qs.iterator():
        summary['datasets_seen'] += 1
        req_tail = _request_input_tail(ds.source_location)
        if not req_tail:
            summary['datasets_unmatched'] += 1
            continue
        matches = [d for d, dtail in indexed if _evgen_input_match(req_tail, dtail)]
        if not matches:
            summary['datasets_unmatched'] += 1
            continue
        summary['datasets_matched'] += 1
        for d in matches:
            matched_dids.add(d['did'])
        entries = [_rucio_evgen_entry(d, checked_at) for d in matches]
        if len(summary['samples']) < 12:
            summary['samples'].append({
                'dataset': ds.did,
                'request': ds.source_location,
                'matched': [e['did'] for e in entries],
            })
        if apply:
            md = dict(ds.metadata or {})
            md['rucio'] = {'matched': entries, 'checked_at': checked_at}
            ds.metadata = md
            try:
                ds.save(update_fields=['metadata'])
            except Exception as e:                            # noqa: BLE001
                summary['errors'].append(f'{ds.did}: save: {e}')

    unmatched_records = [d for d in records if d['did'] not in matched_dids]
    summary['rucio_unmatched'] = len(unmatched_records)
    if apply:
        # Persist the unmatched-Rucio list (EVGEN registered but claimed by no
        # request) onto the current Campaign, the same home the produced-output
        # sweep uses for its rucio_unmatched, so the catalog can surface it.
        unmatched_list = []
        for d in unmatched_records:
            reps = d.get('rse_replicas') or []
            files = max([r.get('length') or 0 for r in reps] or [d.get('length') or 0])
            bytes_ = max([r.get('bytes') or 0 for r in reps] or [d.get('bytes') or 0])
            unmatched_list.append({
                'did': d['did'], 'files': files, 'bytes': bytes_,
                'rses': sorted({r.get('rse', '') for r in reps if r.get('rse')}),
            })
        camp = Campaign.objects.filter(lifecycle='current').first()
        if camp is not None:
            camp.data = {**(camp.data or {}),
                         'evgen_rucio_unmatched': unmatched_list,
                         'evgen_rucio_checked_at': checked_at}
            camp.save(update_fields=['data', 'updated_at'])
            summary['unmatched_persisted'] = len(unmatched_list)
    return summary


def set_pcs_campaign_lifecycle(new_name, target_lifecycle, *, created_by='operator'):
    """Set the PCS Campaign with lifecycle=`target_lifecycle` to `new_name`.

    target_lifecycle is 'current' or 'last' (singular slots — at most one
    Campaign at a time). If the slot is occupied, the existing occupant
    is renamed in place; ProdTask FKs are preserved (same row mutated).
    If empty, a new Campaign(lifecycle=target_lifecycle) is created.
    Operator-initiated only — never call from a sync / refresh handler
    (see feedback-humans-switch-lifecycle).
    """
    if not new_name:
        raise ServiceError('set_pcs_campaign_lifecycle: empty target name')
    if target_lifecycle not in ('current', 'last'):
        raise ServiceError(f'unsupported lifecycle {target_lifecycle!r}')
    existing = (Campaign.objects.filter(lifecycle=target_lifecycle)
                .order_by('-updated_at').first())
    if existing is not None:
        if existing.name == new_name:
            return {'changed': False, 'name': new_name, 'lifecycle': target_lifecycle}
        if Campaign.objects.filter(name=new_name).exclude(pk=existing.pk).exists():
            raise ServiceError(
                f'Campaign named {new_name!r} already exists; '
                f'cannot rename {existing.name!r} into it')
        old_name = existing.name
        existing.name = new_name
        existing.save(update_fields=['name', 'updated_at'])
        _log.info('PCS %s campaign renamed: %s -> %s (by %s)',
                  target_lifecycle, old_name, new_name, created_by)
        return {'changed': True, 'old_name': old_name, 'name': new_name,
                'lifecycle': target_lifecycle, 'created': False}
    # No existing slot — create one. Refuse if any Campaign already uses
    # this name (avoid hijacking past 'FULL/26.04.1' / 'RECO/26.04.1').
    if Campaign.objects.filter(name=new_name).exists():
        raise ServiceError(
            f'Campaign named {new_name!r} already exists; '
            f'cannot create a new {target_lifecycle} with that name')
    Campaign.objects.create(name=new_name, lifecycle=target_lifecycle,
                            created_by=created_by)
    _log.info('PCS %s campaign created: %s (by %s)',
              target_lifecycle, new_name, created_by)
    return {'changed': True, 'old_name': None, 'name': new_name,
            'lifecycle': target_lifecycle, 'created': True}


# Backwards-compat wrapper.
def rename_pcs_current_campaign(new_name, *, created_by='operator'):
    return set_pcs_campaign_lifecycle(new_name, 'current', created_by=created_by)


def prodtask_record_submission(*, task, jedi_task_id, new_status='submitted',
                               panda_tasks_id=None, task_name=None):
    """
    Record outcome of a JEDI submission.

    ``ProdTask.panda_task_id`` is the current/preferred pointer. Full PanDA/JEDI
    history lives in PandaTasks, so recording a different id is a new association,
    not an overwrite error.
    """
    try:
        incoming = int(jedi_task_id)
    except (TypeError, ValueError):
        raise ServiceError('jedi_task_id must be an integer')

    # Commissioning relaxation: recording a submission from a draft task is
    # allowed — the 'ready' freeze is not required. See
    # docs/COMMISSIONING_RELAXATIONS.md.

    valid = _known_prodtask_statuses()
    if new_status not in valid:
        raise ServiceError(
            f'Invalid status. Choose from: {", ".join(sorted(valid))}'
        )
    with transaction.atomic():
        locked = ProdTask.objects.select_for_update().get(pk=task.pk)
        row = None
        if panda_tasks_id:
            row = PandaTasks.objects.select_for_update().filter(
                pk=panda_tasks_id, prod_task=locked).first()
            if row is None:
                raise ServiceError(
                    f'No PandaTasks association {panda_tasks_id} for this task.',
                    status=404,
                )
        if row is None:
            row = PandaTasks.objects.select_for_update().filter(
                jedi_task_id=incoming).first()
            if row and row.prod_task_id != locked.pk:
                raise ServiceError(
                    f'jediTaskID {incoming} is already associated with another task.',
                    status=409,
                )
        if row is None and task_name:
            row = PandaTasks.objects.select_for_update().filter(
                prod_task=locked, task_name=task_name).first()
        if row is None:
            last_try = (
                PandaTasks.objects
                .filter(prod_task=locked)
                .order_by('-try_number')
                .values_list('try_number', flat=True)
                .first()
            )
            try_number = (last_try or 0) + 1
            physical = task_name or panda_tasks_physical_name(locked, try_number)
            row = PandaTasks.objects.create(
                prod_task=locked,
                try_number=try_number,
                task_name=physical,
                out_ds=physical,
                log_ds=_log_dataset_name(physical),
                association_source='record_submission',
                match_reason='created while recording JEDI submission',
            )
        if row.jedi_task_id and row.jedi_task_id != incoming:
            raise ServiceError(
                f'PandaTasks row already records jediTaskID {row.jedi_task_id}; '
                f'cannot overwrite with {incoming}.',
                status=409,
            )
        row.jedi_task_id = incoming
        if not row.out_ds:
            row.out_ds = row.task_name
        if not row.log_ds:
            row.log_ds = _log_dataset_name(row.task_name)
        if not row.association_source:
            row.association_source = 'record_submission'
        row.save(update_fields=[
            'jedi_task_id', 'out_ds', 'log_ds', 'association_source',
            'updated_at',
        ])
        locked.panda_task_id = incoming
        locked.status = new_status
        locked.save(update_fields=['panda_task_id', 'status', 'updated_at'])
        task.panda_task_id = locked.panda_task_id
        task.status = locked.status
    return task


def prodtask_submit_request(*, task):
    """Publish a submit_task request for a locked (ready) task to the prod-ops
    agent. The web tier holds no PanDA credential — it only asks the agent to
    run the submission, which records the jediTaskID back. Gates mirror
    prodtask_record_submission so we never fire a submission whose outcome
    would then be refused. Raises ServiceError on a bad state or an
    unreachable queue. This is the single submit trigger, behind the REST
    `submit` action (compose view + the task detail page's "Submit in Compose"
    link); the legacy page-view submit was retired."""
    import json as _json
    if task.panda_task_id is not None:
        raise ServiceError(
            f'Already submitted as jediTaskID {task.panda_task_id}.', status=409)
    # Commissioning relaxation: a draft task may be submitted directly — the
    # 'ready' freeze is not required. Readiness is surfaced as a non-blocking
    # warning by the caller, not gated here. See docs/COMMISSIONING_RELAXATIONS.md.
    # The Submit button drives the client-API EVGEN doer (submit_evgen_task):
    # noInput+noOutput, payload-staged EVGEN, self-registered RECO. The prun
    # doer ('submit_task', build_panda_command, submit-prod-task.py) is kept but
    # sidelined — not wired to the button. See docs/JEDI_INTEGRATION.md.
    panda_tasks = prodtask_allocate_panda_tasks(task=task)
    msg = {
        'msg_type': 'submit_evgen_task',
        'namespace': 'prodops',
        'task_name': task.name,
        'panda_tasks_id': panda_tasks.pk,
        'panda_task_name': panda_tasks.task_name,
        'owner': task.created_by,
    }
    from monitor_app.activemq_connection import ActiveMQConnectionManager
    try:
        triggered = ActiveMQConnectionManager().send_message(
            '/queue/epicprod.ops', _json.dumps(msg))
    except Exception as e:
        panda_tasks.delete()
        raise ServiceError(f'Could not reach the prod-ops agent queue: {e}', status=503)
    if not triggered:
        panda_tasks.delete()
        raise ServiceError(
            'Submission could not be queued (ops-agent queue unreachable).', status=503)
    return task


def _prodtask_associated_jedi_ids(task):
    ids = []
    if task.panda_task_id:
        ids.append(int(task.panda_task_id))
    try:
        for jedi_task_id in task.panda_tasks.filter(jedi_task_id__isnull=False).values_list('jedi_task_id', flat=True):
            if jedi_task_id and int(jedi_task_id) not in ids:
                ids.append(int(jedi_task_id))
    except Exception:
        # Backward compatibility while old production rows only have
        # ProdTask.panda_task_id.
        pass
    return ids


def prodtask_panda_operation_request(*, task, operation, jedi_task_id=None,
                                     increase=1, new_parameters=None,
                                     created_by='operator'):
    """Queue a PanDA-native operation for an associated existing JEDI task."""
    if operation not in ('increase_attempts', 'retry_failures'):
        raise ServiceError('operation must be increase_attempts or retry_failures')
    ids = _prodtask_associated_jedi_ids(task)
    if not ids:
        raise ServiceError('Task has no associated PanDA/JEDI task.', status=409)
    if jedi_task_id is None:
        jedi_task_id = task.panda_task_id or ids[-1]
    try:
        jedi_task_id = int(jedi_task_id)
    except (TypeError, ValueError):
        raise ServiceError('jedi_task_id must be an integer')
    if jedi_task_id not in ids:
        raise ServiceError(
            f'jediTaskID {jedi_task_id} is not associated with this campaign task.',
            status=409,
        )
    try:
        increase = int(increase or 1)
    except (TypeError, ValueError):
        raise ServiceError('increase must be an integer')
    if increase < 1:
        raise ServiceError('increase must be >= 1')
    if new_parameters is not None and not isinstance(new_parameters, dict):
        raise ServiceError('new_parameters must be a JSON object')

    msg = {
        'msg_type': 'panda_task_operation',
        'namespace': 'prodops',
        'task_name': task.name,
        'composed_name': task.composed_name,
        'jedi_task_id': jedi_task_id,
        'operation': operation,
        'increase': increase,
        'new_parameters': new_parameters or {},
        'created_by': created_by,
    }
    from monitor_app.activemq_connection import ActiveMQConnectionManager
    try:
        triggered = ActiveMQConnectionManager().send_message(
            '/queue/epicprod.ops', _json.dumps(msg))
    except Exception as e:
        raise ServiceError(f'Could not reach the prod-ops agent queue: {e}', status=503)
    if not triggered:
        raise ServiceError(
            'PanDA operation could not be queued (ops-agent queue unreachable).',
            status=503,
        )
    return {'queued': True, 'operation': operation, 'jedi_task_id': jedi_task_id}


def prodtask_rerun_entire_task_request(*, task):
    """Queue a new full PanDA submission attempt for a previously submitted task."""
    if task.panda_task_id is None:
        raise ServiceError(
            'Task has no recorded PanDA submission; use Submit to PanDA.', status=409)
    old_panda_task_id = task.panda_task_id
    old_status = task.status
    task.panda_task_id = None
    task.status = 'draft'
    task.save(update_fields=['panda_task_id', 'status', 'updated_at'])
    try:
        prodtask_submit_request(task=task)
    except Exception:
        task.panda_task_id = old_panda_task_id
        task.status = old_status
        task.save(update_fields=['panda_task_id', 'status', 'updated_at'])
        raise
    return task


def prodtask_record_submission_failure(*, task, panda_tasks_id, reason):
    """Mark a preallocated PandaTasks attempt that failed before JEDI id return."""
    if not panda_tasks_id:
        raise ServiceError('panda_tasks_id is required')
    row = PandaTasks.objects.filter(pk=panda_tasks_id, prod_task=task).first()
    if row is None:
        raise ServiceError(
            f'No PandaTasks association {panda_tasks_id} for this task.',
            status=404,
        )
    meta = dict(row.metadata or {})
    meta['submit_failure'] = reason or ''
    row.status_snapshot = 'submit_failed'
    row.metadata = meta
    row.save(update_fields=['status_snapshot', 'metadata', 'updated_at'])
    return row


def rucio_snapshot_update_request(*, created_by='rucio_snapshot'):
    """Publish a rucio_snapshot_update request to the prod-ops agent, which
    refreshes the JLab Rucio snapshot for the current (+last) campaign and
    rematches produced datasets onto each task's overrides['outputs'] in the
    background, then pushes rucio_snapshot_ready over the SSE relay. The live
    JLab fetch + per-task match is far too slow to run inline in a web request,
    so the web tier only drops the message. Requires a current or last Campaign.
    Raises ServiceError on a bad state or an unreachable queue. See
    docs/EPICPROD_DATA_LINEAGE.md, docs/EPICPROD_OPS_AGENT.md."""
    import json as _json
    from .models import Campaign
    if not Campaign.objects.filter(lifecycle__in=['current', 'last']).exists():
        raise ServiceError('No current Campaign defined in PCS.', status=400)
    msg = {'msg_type': 'rucio_snapshot_update', 'namespace': 'prodops',
           'created_by': created_by}
    from monitor_app.activemq_connection import ActiveMQConnectionManager
    try:
        triggered = ActiveMQConnectionManager().send_message(
            '/queue/epicprod.ops', _json.dumps(msg))
    except Exception as e:
        raise ServiceError(
            f'Could not reach the prod-ops agent queue: {e}', status=503)
    if not triggered:
        raise ServiceError(
            'Rucio update could not be queued (ops-agent queue unreachable).',
            status=503)


def evgen_rucio_update_request(*, created_by='evgen_rucio'):
    """Publish an evgen_rucio_update request to the prod-ops agent, which
    assimilates the JLab Rucio EVGEN inventory (epic:/EVGEN/*) and resolves each
    PCS evgen Dataset onto metadata['rucio'] in the background, then pushes
    evgen_rucio_ready over the SSE relay. The live JLab fetch + per-dataset match
    is too slow to run inline in a web request, so the web tier only drops the
    message. EVGEN is not campaign-bound, so there is no Campaign precondition.
    Raises ServiceError if the queue is unreachable. See
    docs/EPICPROD_EVGEN_INPUTS.md, docs/EPICPROD_OPS_AGENT.md."""
    import json as _json
    msg = {'msg_type': 'evgen_rucio_update', 'namespace': 'prodops',
           'created_by': created_by}
    from monitor_app.activemq_connection import ActiveMQConnectionManager
    try:
        triggered = ActiveMQConnectionManager().send_message(
            '/queue/epicprod.ops', _json.dumps(msg))
    except Exception as e:
        raise ServiceError(
            f'Could not reach the prod-ops agent queue: {e}', status=503)
    if not triggered:
        raise ServiceError(
            'EVGEN update could not be queued (ops-agent queue unreachable).',
            status=503)


def questionnaire_match_update_request(*, created_by='questionnaire_match'):
    """Publish a questionnaire_match_update request to the prod-ops agent.

    The agent rebuilds the task-local inverse cache from
    Questionnaire.data['prod_matches'] and then pushes
    questionnaire_match_ready over the SSE relay. Catalog rendering reads the
    cache from task overrides and does not scan questionnaires inline.
    """
    import json as _json
    msg = {'msg_type': 'questionnaire_match_update', 'namespace': 'prodops',
           'created_by': created_by}
    from monitor_app.activemq_connection import ActiveMQConnectionManager
    try:
        triggered = ActiveMQConnectionManager().send_message(
            '/queue/epicprod.ops', _json.dumps(msg))
    except Exception as e:
        raise ServiceError(
            f'Could not reach the prod-ops agent queue: {e}', status=503)
    if not triggered:
        raise ServiceError(
            'Questionnaire match update could not be queued '
            '(ops-agent queue unreachable).',
            status=503)


def campaign_progress_refresh_request(*, created_by='progress_refresh'):
    """Publish a campaign_progress_refresh request to the prod-ops agent.

    The agent rebuilds the current campaign's PanDA progress snapshot and the
    rendered progress table cache, then pushes campaign_progress_ready over SSE.
    Page GETs only read cache.
    """
    import json as _json
    if not Campaign.objects.filter(lifecycle='current').exists():
        raise ServiceError('No current Campaign defined in PCS.', status=400)
    msg = {'msg_type': 'campaign_progress_refresh', 'namespace': 'prodops',
           'created_by': created_by}
    from monitor_app.activemq_connection import ActiveMQConnectionManager
    try:
        triggered = ActiveMQConnectionManager().send_message(
            '/queue/epicprod.ops', _json.dumps(msg))
    except Exception as e:
        raise ServiceError(
            f'Could not reach the prod-ops agent queue: {e}', status=503)
    if not triggered:
        raise ServiceError(
            'Progress refresh could not be queued (ops-agent queue unreachable).',
            status=503)


def catalog_import_request(source, *, created_by='catalog_import'):
    """Publish a catalog import request to the prod-ops agent, which runs the
    import in the background and pushes catalog_import_ready over the SSE relay.
    The epic-prod past import walks ~4900 datasets — too slow to run inline in a
    web request (it times the gateway out), so the web tier only drops the
    message. ``source`` is 'csv' (the current default_datasets.csv) or
    'epic-prod' (the past/future FULL+RECO output). Raises ServiceError on a bad
    source or an unreachable queue. See docs/EPICPROD_OPS_AGENT.md, SSE_PUSH.md."""
    import json as _json
    if source not in ('csv', 'epic-prod'):
        raise ServiceError(f'Unknown catalog import source {source!r}.', status=400)
    msg = {'msg_type': 'catalog_import', 'source': source,
           'namespace': 'prodops', 'created_by': created_by}
    from monitor_app.activemq_connection import ActiveMQConnectionManager
    try:
        triggered = ActiveMQConnectionManager().send_message(
            '/queue/epicprod.ops', _json.dumps(msg))
    except Exception as e:
        raise ServiceError(
            f'Could not reach the prod-ops agent queue: {e}', status=503)
    if not triggered:
        raise ServiceError(
            'Catalog import could not be queued (ops-agent queue unreachable).',
            status=503)
