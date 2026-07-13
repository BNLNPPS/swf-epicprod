"""V1 analytics members (docs/EPICPROD_ASSESSMENTS_V1.md).

Each member is ``compute(campaign, window_start, window_end) -> block``:

    {"member": ..., "schema_version": 1, "computed_at": <iso8601>,
     "window": {"start": ..., "end": ...}, "data": {...}}

V1 members wrap computations the system already performs — the cached
progress snapshot, the precomputed arrivals timeline, dataset
dispositions, and the epicprod action stream. A member whose source is
absent reports ``available: false`` with the reason rather than raising:
the rollup must always assemble, and missing evidence is itself evidence.
"""

import datetime as _dt

from django.utils import timezone

# Bounds keep the rollup a bounded evidence document, not a dump.
LEAST_COMPLETE_LIMIT = 10
DISPOSITION_FLIPS_LIMIT = 20
TOP_ERRORS_LIMIT = 5
TIMELINE_TAIL_BINS = 14
ACTION_ROWS_MAX = 5000
ASSESSMENT_ACTIONS = {
    'assessment_triggered', 'assessment_enforce', 'assessment_register',
    'assessment_retry', 'assessment_completed',
}
INTERNAL_ACTIONS = {'campaign_analytics_snapshot'}


def _block(member, window_start, window_end, data):
    return {
        'member': member,
        'schema_version': 1,
        'computed_at': timezone.now().isoformat(),
        'window': {'start': window_start.isoformat(),
                   'end': window_end.isoformat()},
        'data': data,
    }


def _unavailable(reason):
    return {'available': False, 'reason': reason}


def campaign_progress(campaign, window_start, window_end):
    """Aggregate the cached PCS progress view by unique output DID."""
    from pcs.services import load_campaign_progress_snapshot

    snap = load_campaign_progress_snapshot(campaign)
    if not snap:
        return _block('campaign_progress', window_start, window_end,
                      _unavailable('no progress snapshot cached; the nightly '
                                   'chain or the progress-refresh button '
                                   'builds it'))

    rows = list((snap.get('rows') or {}).values())
    unique_outputs = {}
    duplicate_dids = {}
    anonymous = 0
    for row in rows:
        for output in row.get('outputs') or []:
            did = str(output.get('did') or '').strip()
            if did:
                key = f'did:{did}'
            else:
                anonymous += 1
                key = f'anonymous:{row.get("task_id")}:{anonymous}'
            prior = unique_outputs.get(key)
            if prior is not None and did:
                duplicate_dids[did] = duplicate_dids.get(did, 1) + 1
            checked = str(output.get('checked_at') or '')
            prior_checked = str((prior or {}).get('checked_at') or '')
            if prior is None or checked >= prior_checked:
                unique_outputs[key] = {
                    **output,
                    '_task_name': row.get('task_name') or '',
                }

    total_files = 0
    total_bytes = 0
    outputs_total = 0
    outputs_complete = 0
    incomplete = []
    for output in unique_outputs.values():
        outputs_total += 1
        total_files += int(output.get('file_count') or 0)
        total_bytes += int(output.get('bytes') or 0)
        pct = output.get('completion_percent')
        if output.get('complete') and (pct is None or pct >= 100):
            outputs_complete += 1
        else:
            incomplete.append({
                'task_name': output.get('_task_name') or '',
                'did': output.get('did') or '',
                'completion_percent': pct,
                'file_count': int(output.get('file_count') or 0),
                'expected_jobs': output.get('expected_jobs'),
            })
    incomplete.sort(key=lambda o: (o['completion_percent'] is None,
                                   o['completion_percent'] or 0))
    return _block('campaign_progress', window_start, window_end, {
        'available': True,
        'source_generated_at': snap.get('generated_at') or '',
        'task_count': snap.get('task_count') or len(rows),
        'tasks_with_processing': sum(1 for r in rows if r.get('has_processing')),
        'outputs_total': outputs_total,
        'outputs_complete': outputs_complete,
        'total_files': total_files,
        'total_bytes': total_bytes,
        'output_identity': 'unique DID; outputs without a DID remain distinct',
        'duplicate_output_records': sum(n - 1 for n in duplicate_dids.values()),
        'duplicate_dids': [
            {'did': did, 'records': count}
            for did, count in sorted(duplicate_dids.items())[:10]
        ],
        'duplicate_dids_truncated': max(0, len(duplicate_dids) - 10),
        'least_complete': incomplete[:LEAST_COMPLETE_LIMIT],
        'least_complete_truncated': max(0, len(incomplete) - LEAST_COMPLETE_LIMIT),
        'source_errors': snap.get('errors') or [],
    })


def _campaign_panda_task_ids(campaign):
    """Campaign PanDA population from PCS associations plus name discovery."""
    from django.db import connections
    from monitor_app.panda.constants import PANDA_SCHEMA
    from pcs.models import PandaTasks, ProdTask

    associated = set(
        PandaTasks.objects
        .filter(prod_task__campaign=campaign, jedi_task_id__isnull=False)
        .values_list('jedi_task_id', flat=True)
    )
    associated.update(
        ProdTask.objects
        .filter(campaign=campaign, panda_task_id__isnull=False)
        .values_list('panda_task_id', flat=True)
    )
    with connections['panda'].cursor() as cursor:
        cursor.execute(
            f'SELECT "jeditaskid" FROM "{PANDA_SCHEMA}"."jedi_tasks" '
            f'WHERE "taskname" LIKE %s',
            [f'group.EIC.{campaign.name}.%'])
        name_matched = {row[0] for row in cursor.fetchall()}
    return sorted(associated | name_matched), {
        'associated': len(associated),
        'name_matched': len(name_matched),
        'union': len(associated | name_matched),
    }


def panda_health(campaign, window_start, window_end):
    """PanDA job-state aggregate over every campaign-named task.

    The population is all JEDI tasks whose name carries the campaign
    identity — including aborted and failed early attempts, which the
    progress snapshot deliberately omits (they have no outputs) but which
    carry exactly the failures a health axis must count. Found by the
    first assessment's own audit: 40 campaign-named tasks vs 32
    snapshot-matched (2026-07-12).
    """
    from pcs.services import _panda_progress_summaries

    try:
        task_ids, population = _campaign_panda_task_ids(campaign)
    except Exception as e:
        return _block('panda_health', window_start, window_end,
                      _unavailable(f'PanDA task lookup failed: {e}'))
    if not task_ids:
        return _block('panda_health', window_start, window_end,
                      _unavailable('no campaign-named PanDA tasks'))

    by_id, _by_name, errors = _panda_progress_summaries(task_ids, [])
    if errors:
        return _block('panda_health', window_start, window_end,
                      _unavailable('; '.join(str(e) for e in errors)))
    seen = by_id

    sums = {k: 0 for k in ('nactive', 'nfinished', 'nfailed',
                           'nfinalfailed', 'nrunning', 'total_jobs')}
    statuses = {}
    errors = {}
    for summary in seen.values():
        for key in sums:
            sums[key] += int(summary.get(key) or 0)
        status = str(summary.get('status') or 'unknown')
        statuses[status] = statuses.get(status, 0) + 1
        dialog = str(summary.get('errordialog') or '').strip()
        if dialog:
            errors[dialog[:200]] = errors.get(dialog[:200], 0) + 1

    terminal = sums['nfinalfailed'] + sums['nfinished']
    rate = round(sums['nfinalfailed'] / terminal, 4) if terminal else None
    top_errors = sorted(errors.items(), key=lambda kv: -kv[1])[:TOP_ERRORS_LIMIT]
    return _block('panda_health', window_start, window_end, {
        'available': True,
        'panda_task_count': len(seen),
        'population': population,
        'task_statuses': statuses,
        'jobs': sums,
        'final_failure_rate': rate,
        'top_errors': [{'error': e, 'tasks': n} for e, n in top_errors],
    })


def rucio_arrivals(campaign, window_start, window_end):
    """JLab file-arrival sweeps and dataset-first-arrival history.

    These are separate measurements. File sweeps count newly created file
    DIDs over their recorded sweep intervals. The timeline records the first
    replica arrival for each dataset and is retained only as lifetime context.
    """
    from monitor_app.models import AppLog
    from pcs.services import load_rucio_timeline

    arrivals = (campaign.data or {}).get('arrivals') or {}
    last_at = arrivals.get('last_arrival_at') or ''
    age_hours = None
    if last_at:
        try:
            age_hours = round(
                (timezone.now() - _dt.datetime.fromisoformat(last_at)
                 .replace(tzinfo=_dt.timezone.utc)).total_seconds() / 3600, 1)
        except (TypeError, ValueError):
            age_hours = None

    sweeps = []
    rows = (
        AppLog.objects
        .filter(app_name='epicprod',
                extra_data__action='rucio_arrivals',
                timestamp__gte=window_start,
                timestamp__lt=window_end)
        .order_by('timestamp')
        .values('timestamp', 'extra_data')
    )
    for row in rows:
        extra = row['extra_data'] if isinstance(row['extra_data'], dict) else {}
        count = int((extra.get('campaigns') or {}).get(campaign.name) or 0)
        if not count:
            continue
        sweeps.append({
            'window_start': str(extra.get('window_start') or ''),
            'window_end': row['timestamp'].isoformat(),
            'files': count,
            'roots': list(extra.get('roots') or []),
        })

    data = {
        'available': bool(arrivals or sweeps),
        'measurement': 'newly created JLab Rucio file DIDs',
        'last_arrival_at': last_at,
        'last_arrival_age_hours': age_hours,
        'file_sweeps_ending_in_window': sweeps,
        'files_in_recorded_sweeps': sum(row['files'] for row in sweeps),
        'latest_sweep': {
            key: value for key, value in arrivals.items()
            if key != 'locations'
        },
    }
    timeline = load_rucio_timeline(campaign.name)
    if timeline:
        dates = timeline.get('dates') or []
        tail = slice(max(0, len(dates) - TIMELINE_TAIL_BINS), len(dates))
        series = {'dates': dates[tail]}
        for stage in ('reco', 'simu'):
            cum_files = (timeline.get(stage) or {}).get('cum_files') or []
            cum_bytes = (timeline.get(stage) or {}).get('cum_bytes') or []
            if cum_files:
                series[f'{stage}_cum_files'] = cum_files[tail]
            if cum_bytes:
                series[f'{stage}_cum_bytes'] = cum_bytes[tail]
        data['dataset_first_arrival_timeline'] = {
            'measurement': (
                'dataset file count assigned at the dataset earliest replica '
                'creation time; later file additions do not move the series'),
            'snapshot_fetched_at': timeline.get('snapshot_fetched_at') or '',
            'tail': series,
        }
    if not arrivals and not sweeps and not timeline:
        data.update(_unavailable('no arrivals recorded for this campaign'))
    return _block('rucio_arrivals', window_start, window_end, data)


def disposition_mix(campaign, window_start, window_end):
    """Propagation dispositions of the campaign's datasets, with recent flips."""
    from pcs.models import Dataset

    counts = {}
    flips = []
    for ds in Dataset.objects.filter(campaign=campaign).only(
            'composed_name', 'propagation', 'metadata'):
        state = ds.propagation or 'continue'
        counts[state] = counts.get(state, 0) + 1
        history = (((ds.metadata or {}).get('propagation') or {})
                   .get('history') or [])
        for entry in history:
            changed_at = str(entry.get('changed_at') or '')
            try:
                changed = _dt.datetime.fromisoformat(
                    changed_at.replace('Z', '+00:00'))
                if changed.tzinfo is None:
                    changed = changed.replace(tzinfo=_dt.timezone.utc)
            except (TypeError, ValueError):
                continue
            if not (window_start <= changed < window_end):
                continue
            flips.append({
                'dataset': ds.composed_name,
                'state': entry.get('state') or '',
                'previous': entry.get('previous') or '',
                'comment': str(entry.get('comment') or '')[:200],
                'changed_by': entry.get('changed_by') or '',
                'changed_at': changed.isoformat(),
            })
    flips.sort(key=lambda f: f['changed_at'], reverse=True)
    return _block('disposition_mix', window_start, window_end, {
        'available': True,
        'dispositions': counts,
        'window_flips': flips[:DISPOSITION_FLIPS_LIMIT],
        'window_flips_truncated': max(0, len(flips) - DISPOSITION_FLIPS_LIMIT),
    })


def action_stream_activity(campaign, window_start, window_end):
    """epicprod action-stream aggregate over the window, plus chain freshness."""
    from monitor_app.models import AppLog

    rows = list(
        AppLog.objects
        .filter(app_name='epicprod', timestamp__gte=window_start,
                timestamp__lt=window_end)
        .order_by('-timestamp')
        .values('timestamp', 'extra_data')[:ACTION_ROWS_MAX]
    )
    by_action = {}
    campaign_by_action = {}
    assessment_by_action = {}
    for row in rows:
        extra = row['extra_data'] if isinstance(row['extra_data'], dict) else {}
        action = str(extra.get('action') or 'unknown')
        outcome = str(extra.get('outcome') or '')
        entry = by_action.setdefault(action, {'count': 0, 'errors': 0})
        entry['count'] += 1
        if outcome and outcome != 'ok':
            entry['errors'] += 1
        subject_key = str(extra.get('subject_key') or '')
        is_campaign = (
            (extra.get('subject_type') == 'campaign'
             and subject_key == campaign.name)
            or str(extra.get('campaign') or '') == campaign.name
            or f'.{campaign.name}.' in subject_key
            or subject_key.startswith(f'{campaign.name}/')
        )
        if is_campaign:
            target = (assessment_by_action if action in ASSESSMENT_ACTIONS
                      else campaign_by_action)
            if action in INTERNAL_ACTIONS:
                continue
            campaign_entry = target.setdefault(
                action, {'count': 0, 'errors': 0})
            campaign_entry['count'] += 1
            if outcome and outcome != 'ok':
                campaign_entry['errors'] += 1

    sync = (
        AppLog.objects
        .filter(app_name='epicprod', extra_data__action='catalog_sync')
        .order_by('-timestamp')
        .values('timestamp', 'extra_data')
        .first()
    )
    sync_age_hours = None
    sync_outcome = ''
    if sync:
        sync_age_hours = round(
            (timezone.now() - sync['timestamp']).total_seconds() / 3600, 1)
        extra = sync['extra_data'] if isinstance(sync['extra_data'], dict) else {}
        sync_outcome = str(extra.get('outcome') or '')
    return _block('action_stream_activity', window_start, window_end, {
        'available': True,
        # Keep campaign-attributed activity separate from shared platform
        # mechanics so a campaign report cannot silently claim global work.
        'actions': campaign_by_action,
        'assessment_actions': assessment_by_action,
        'system_actions': by_action,
        'window_rows': len(rows),
        'window_truncated': len(rows) >= ACTION_ROWS_MAX,
        'catalog_sync_age_hours': sync_age_hours,
        'catalog_sync_outcome': sync_outcome,
    })


def window_activity(campaign, window_start, window_end):
    """The window's own PanDA activity — the daily report's subject.

    Jobs that reached a state in the window (by site and status) and
    campaign-named tasks created or newly terminal in it, so productivity
    or its absence is measured on the window, not the campaign's lifetime.
    """
    from django.db import connections
    from monitor_app.panda.constants import PANDA_SCHEMA

    try:
        task_ids, population = _campaign_panda_task_ids(campaign)
        with connections['panda'].cursor() as cursor:
            tasks = []
            jobs_by = {}
            if task_ids:
                marks = ','.join(['%s'] * len(task_ids))
                cursor.execute(
                    f'SELECT "jeditaskid", "taskname", "status", '
                    f'"creationdate", "modificationtime" '
                    f'FROM "{PANDA_SCHEMA}"."jedi_tasks" '
                    f'WHERE "jeditaskid" IN ({marks})', task_ids)
                tasks = cursor.fetchall()
                for table in ('jobsarchived4', 'jobsactive4'):
                    cursor.execute(
                        f'SELECT "computingsite", "jobstatus", COUNT(*) '
                        f'FROM "{PANDA_SCHEMA}"."{table}" '
                        f'WHERE "jeditaskid" IN ({marks}) '
                        f'AND "modificationtime" >= %s '
                        f'AND "modificationtime" < %s '
                        f'GROUP BY 1, 2',
                        task_ids + [window_start, window_end])
                    for site, status, n in cursor.fetchall():
                        entry = jobs_by.setdefault(site or 'unknown', {})
                        entry[status] = entry.get(status, 0) + int(n)
    except Exception as e:
        return _block('window_activity', window_start, window_end,
                      _unavailable(f'PanDA window query failed: {e}'))

    initiated = []
    completed = []
    failed = []
    for tid, name, status, created, modified in tasks:
        if (created and window_start.replace(tzinfo=None) <= created
                < window_end.replace(tzinfo=None)):
            initiated.append({'jeditaskid': tid, 'name': name, 'status': status})
        if (modified and window_start.replace(tzinfo=None) <= modified
                < window_end.replace(tzinfo=None)
                and status in ('done', 'finished', 'failed', 'aborted', 'broken')):
            entry = {'jeditaskid': tid, 'name': name, 'status': status}
            (failed if status in ('failed', 'aborted', 'broken')
             else completed).append(entry)

    window_finished = sum(e.get('finished', 0) for e in jobs_by.values())
    window_failed = sum(e.get('failed', 0) for e in jobs_by.values())
    terminal = window_finished + window_failed
    jobs_by_status = {}
    for states in jobs_by.values():
        for status, count in states.items():
            jobs_by_status[status] = jobs_by_status.get(status, 0) + count
    return _block('window_activity', window_start, window_end, {
        'available': True,
        'population': population,
        'jobs_by_site': jobs_by,
        'jobs_by_status': jobs_by_status,
        'window_jobs_finished': window_finished,
        'window_jobs_failed': window_failed,
        'window_failure_rate': (round(window_failed / terminal, 4)
                                if terminal else None),
        'tasks_initiated': initiated[:20],
        'tasks_initiated_truncated': max(0, len(initiated) - 20),
        'tasks_completed': completed[:20],
        'tasks_completed_truncated': max(0, len(completed) - 20),
        'tasks_newly_failed': failed[:20],
        'tasks_newly_failed_truncated': max(0, len(failed) - 20),
        'quiet': not jobs_by and not initiated and not completed and not failed,
    })


def system_status(campaign, window_start, window_end):
    """Cached platform system status — the System page's source summary.

    Read in-process (the cache rows the ops agent refreshes), so the
    assessment bundle needs no separately authenticated fetch.
    """
    from monitor_app.viewdir.system_status import status_summary

    summary = status_summary()
    latest = summary.get('latest_checked_at')
    return _block('system_status', window_start, window_end, {
        'available': bool(summary.get('total')),
        'overall_status': summary.get('overall_status', 'unknown'),
        'overall_reason': summary.get('overall_reason', ''),
        'latest_checked_at': latest.isoformat() if latest else None,
        'counts': {k: summary.get(k, 0)
                   for k in ('ok', 'warning', 'error', 'unknown', 'total')},
    })


def credential_status(campaign, window_start, window_end):
    """Latest credential expiry check from the action stream."""
    from monitor_app.models import AppLog

    row = (
        AppLog.objects
        .filter(app_name='epicprod',
                extra_data__action='credential_expiry_check')
        .order_by('-timestamp')
        .values('timestamp', 'message', 'extra_data')
        .first()
    )
    if not row:
        return _block('credential_status', window_start, window_end,
                      _unavailable('no credential_expiry_check record found'))
    extra = row['extra_data'] if isinstance(row['extra_data'], dict) else {}
    known = ('action', 'subject_type', 'subject_key', 'username', 'outcome',
             'reason', 'duration_ms', 'sublevel', 'live_default')
    return _block('credential_status', window_start, window_end, {
        'available': True,
        'checked_at': row['timestamp'].isoformat(),
        'age_hours': round(
            (timezone.now() - row['timestamp']).total_seconds() / 3600, 1),
        'outcome': str(extra.get('outcome') or ''),
        'reason': str(extra.get('reason') or ''),
        'message': str(row['message'] or '')[:500],
        'details': {k: v for k, v in extra.items() if k not in known},
    })


MEMBERS = (
    campaign_progress,
    panda_health,
    window_activity,
    rucio_arrivals,
    disposition_mix,
    action_stream_activity,
    system_status,
    credential_status,
)
