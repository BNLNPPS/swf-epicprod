"""Deterministic campaign-report facts and Markdown assembly.

The model returns bounded judgment as structured data. This module owns the
human report: production facts come directly from the evidence bundle, while
the model's assessment, issues, software findings, and outlook are inserted
only in their named sections.
"""

import re
from datetime import datetime
from zoneinfo import ZoneInfo


FACTS_SCHEMA = 'epicprod-report-facts/2'
ET = ZoneInfo('America/New_York')


def _member(rollup, name):
    return ((((rollup or {}).get('members') or {}).get(name) or {})
            .get('data') or {})


def _number(value):
    return f'{int(value or 0):,}'


def _bytes(value):
    size = float(value or 0)
    units = ('B', 'KB', 'MB', 'GB', 'TB', 'PB')
    for unit in units:
        if abs(size) < 1000 or unit == units[-1]:
            return f'{size:,.1f} {unit}' if unit != 'B' else f'{int(size):,} B'
        size /= 1000


def _timestamp(value):
    if not value:
        return ''
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return str(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ET)
    return parsed.astimezone(ET).strftime('%Y-%m-%d %H:%M %Z')


def _interval(start, end):
    return f'{_timestamp(start)} to {_timestamp(end)}'


def _delta(deltas, name):
    entry = (deltas or {}).get(name)
    if not isinstance(entry, dict):
        return ''
    change = entry.get('delta')
    if change is None:
        return ''
    elapsed = (deltas or {}).get('elapsed_hours')
    span = f' over {elapsed:g} h' if isinstance(elapsed, (int, float)) else ''
    return f'{int(change):+,}{span}'


def _bytes_delta(deltas, name):
    entry = (deltas or {}).get(name)
    if not isinstance(entry, dict) or entry.get('delta') is None:
        return ''
    change = int(entry.get('delta') or 0)
    sign = '+' if change >= 0 else '-'
    elapsed = (deltas or {}).get('elapsed_hours')
    span = f' over {elapsed:g} h' if isinstance(elapsed, (int, float)) else ''
    return f'{sign}{_bytes(abs(change))}{span}'


def _comparisons(*items):
    return '; '.join(item for item in items if item)


def _disposition_changes(deltas):
    changed = (deltas or {}).get('dispositions_changed') or {}
    return '; '.join(
        f'{state} {_number(item.get("previous"))}→{_number(item.get("current"))}'
        for state, item in sorted(changed.items()))


def _row(row_id, fact, value, comparison='', source=''):
    return {
        'id': row_id,
        'fact': fact,
        'value': value,
        'comparison': comparison,
        'source': source,
    }


def build_fact_set(rollup, deltas):
    """Normalize report facts once for both the model and final renderer."""
    rollup = rollup or {}
    window = rollup.get('window') or {}
    activity = []
    current = []
    notes = []
    problems = []

    panda_window = _member(rollup, 'window_activity')
    if panda_window.get('available'):
        panda_population = int(
            panda_window.get('task_population')
            or (panda_window.get('population') or {}).get('union')
            or 0)
        panda_scope = f'campaign PanDA population: {panda_population} tasks'
        task_counts = {
            'initiated': len(panda_window.get('tasks_initiated') or []),
            'completed': len(panda_window.get('tasks_completed') or []),
            'newly failed': len(panda_window.get('tasks_newly_failed') or []),
        }
        activity.append(_row(
            'panda_task_activity',
            f'Campaign PanDA task transitions ({panda_population} tasks)',
            '; '.join(f'{_number(value)} {label}'
                      for label, value in task_counts.items()),
            comparison=(
                'last campaign-task change '
                + _timestamp(panda_window.get('last_task_modification_at'))
                if panda_window.get('last_task_modification_at') else ''),
            source=f'direct PanDA DB window query; {panda_scope}'))
        job_states = panda_window.get('jobs_by_status') or {}
        if job_states:
            jobs = '; '.join(
                f'{_number(count)} {status}'
                for status, count in sorted(job_states.items()))
        else:
            jobs = '0 jobs modified in the interval'
        activity.append(_row(
            'panda_job_activity',
            f'Campaign PanDA job activity ({panda_population} tasks)', jobs,
            comparison=(
                'last campaign-job change '
                + _timestamp(panda_window.get('last_job_modification_at'))
                if panda_window.get('last_job_modification_at') else ''),
            source=f'direct PanDA DB modification-time window; {panda_scope}'))
        jobs_by_site = panda_window.get('jobs_by_site') or {}
        if jobs_by_site:
            activity.append(_row(
                'panda_site_activity',
                f'Campaign PanDA activity by site ({panda_population} tasks)',
                '; '.join(
                    f'{site}: {_number(sum(states.values()))} jobs'
                    for site, states in sorted(
                        jobs_by_site.items(),
                        key=lambda item: (-sum(item[1].values()), item[0]))),
                source=f'direct PanDA DB modification-time window; {panda_scope}'))
    else:
        problems.append('PanDA window activity unavailable: '
                        + str(panda_window.get('reason') or 'unknown reason'))

    usage = _member(rollup, 'resource_usage')
    if usage.get('available'):
        usage_population = int(
            usage.get('task_population')
            or (usage.get('population') or {}).get('union')
            or 0)
        totals = usage.get('totals') or {}
        efficiency = totals.get('allocation_efficiency')
        activity.append(_row(
            'panda_resource_usage',
            f'Campaign PanDA resource usage ({usage_population} tasks)',
            f'{_number(totals.get("job_count"))} finished jobs; '
            f'{float(totals.get("allocated_core_hours") or 0):,.1f} allocated '
            'core-hours; '
            f'{float(totals.get("used_core_hours") or 0):,.1f} used core-hours'
            + (f'; {float(efficiency):.1%} allocation efficiency'
               if efficiency is not None else ''),
            source=(
                'direct PanDA DB runtime and CPU accounting for the interval; '
                f'campaign PanDA population: {usage_population} tasks')))
    else:
        problems.append('PanDA resource usage unavailable: '
                        + str(usage.get('reason') or 'unknown reason'))

    arrivals = _member(rollup, 'rucio_arrivals')
    sweeps = arrivals.get('file_sweeps_ending_in_window') or []
    action_stream = _member(rollup, 'action_stream_activity')
    sweep_ran = bool((action_stream.get('system_actions') or {})
                     .get('rucio_arrivals_sweep'))
    if sweeps or sweep_ran:
        total = int(arrivals.get('files_in_recorded_sweeps') or 0)
        latest = arrivals.get('latest_sweep') or {}
        by_root = latest.get('by_root') or {}
        detail = ', '.join(
            f'{root} {_number(count)}' for root, count in sorted(by_root.items()))
        value = f'{_number(total)} newly created file DIDs'
        if len(sweeps) == 1 and detail:
            value += f' ({detail})'
        sweep_coverage = arrivals.get('sweep_coverage') or {}
        coverage = ''
        if sweep_coverage:
            overlap = sweep_coverage.get('report_window_overlap_hours')
            report_hours = sweep_coverage.get('report_window_hours')
            gap = sweep_coverage.get('report_window_gap_hours')
            outside = sweep_coverage.get('outside_report_window_hours')
            coverage = (
                f'recorded sweeps overlap {overlap:g} of {report_hours:g} '
                'report hours')
            if gap:
                coverage += f'; {gap:g} report hours uncovered'
            if outside:
                coverage += f'; {outside:g} measured hours outside the interval'
        activity.append(_row(
            'jlab_rucio_file_arrivals',
            'Campaign-attributed JLab Rucio file arrivals', value,
            comparison=coverage,
            source='file-DID created-after sweep'))
        locations = latest.get('locations') or {}
        if locations:
            activity.append(_row(
                'jlab_rucio_latest_arrival_locations',
                'Locations changed in the latest JLab Rucio sweep',
                '; '.join(
                    f'{location}: {_number(count)} files'
                    for location, count in sorted(locations.items())),
                source='campaign arrivals record'))
    else:
        activity.append(_row(
            'jlab_rucio_file_arrivals',
            'Campaign-attributed JLab Rucio file arrivals',
            'No file-arrival sweep ended in the interval',
            source='epicprod action stream'))

    dispositions = _member(rollup, 'disposition_mix')
    if dispositions.get('available'):
        flips = dispositions.get('window_flips') or []
        activity.append(_row(
            'disposition_changes', 'Dataset disposition changes',
            f'{_number(len(flips))} changes',
            source='PCS disposition history'))

    actions = action_stream.get('actions') or {}
    if actions:
        value = '; '.join(
            f'{action} {_number(item.get("count"))}'
            + (f' ({_number(item.get("errors"))} errors)'
               if item.get('errors') else '')
            for action, item in sorted(actions.items()))
        activity.append(_row(
            'production_actions', 'Production automation', value,
            source='epicprod action stream; assessment actions excluded'))
    else:
        activity.append(_row(
            'production_actions', 'Production automation',
            'No campaign-attributed production action recorded',
            source='epicprod action stream; assessment actions excluded'))
    system = _member(rollup, 'system_status')
    history = system.get('window_history') or {}
    if history.get('observations'):
        non_ok_by_check = history.get('non_ok_by_check') or {}
        non_ok_count = int(history.get('non_ok_observations_count') or 0)
        if non_ok_by_check:
            detail = '; '.join(
                f'{name} {_number(count)}'
                for name, count in sorted(non_ok_by_check.items()))
            value = (
                f'{_number(history.get("observations"))} observations; '
                f'{_number(non_ok_count)} non-OK ({detail})')
        else:
            value = (
                f'{_number(history.get("observations"))} observations; '
                'no non-OK observation')
        recovery = ''
        if non_ok_count:
            recovery = (
                f'{_number(history.get("recovered_count"))} recovered; '
                f'{_number(history.get("unresolved_count"))} unresolved')
            longest = history.get('longest_recovery_minutes')
            if longest is not None:
                recovery += f'; longest recovery {float(longest):.1f} min'
        activity.append(_row(
            'platform_window_history', 'Production platform observations',
            value,
            comparison=recovery,
            source='append-only system-status history for the interval'))

    progress = _member(rollup, 'campaign_progress')
    if progress.get('available'):
        source_built_at = _timestamp(progress.get('source_generated_at'))
        observed_start = _timestamp(progress.get('source_observed_start'))
        observed_end = _timestamp(progress.get('source_observed_end'))
        if observed_start and observed_end:
            observed_at = (observed_start if observed_start == observed_end
                           else f'{observed_start} to {observed_end}')
        else:
            observed_at = observed_start or observed_end or 'unknown time'
        output_source = (
            f'PCS output records observed {observed_at}; '
            f'progress cache built {source_built_at}')
        task_source = f'PCS task/processing view built {source_built_at}'
        current.extend([
            _row(
                'pcs_campaign_tasks', 'PCS campaign tasks',
                f'{_number(progress.get("task_count"))} catalog rows; '
                f'{_number(progress.get("tasks_with_processing"))} with processing',
                comparison=_comparisons(
                    ('tasks ' + _delta(deltas, 'task_count'))
                    if _delta(deltas, 'task_count') else '',
                    ('with processing '
                     + _delta(deltas, 'tasks_with_processing'))
                    if _delta(deltas, 'tasks_with_processing') else ''),
                source=task_source),
            _row(
                'pcs_output_placement', 'PCS Rucio placement',
                f'{_number(progress.get("outputs_placement_complete"))} of '
                f'{_number(progress.get("outputs_total"))} unique outputs '
                'fully placed; '
                f'{_number(progress.get("outputs_placement_incomplete"))} '
                'incompletely placed',
                comparison=_comparisons(
                    ('fully placed '
                     + _delta(deltas, 'outputs_placement_complete'))
                    if _delta(deltas, 'outputs_placement_complete') else '',
                    ('total ' + _delta(deltas, 'outputs_total'))
                    if _delta(deltas, 'outputs_total') else ''),
                source=output_source),
            _row(
                'pcs_output_volume', 'PCS recorded output volume',
                f'{_number(progress.get("total_files"))} files; '
                f'{_bytes(progress.get("total_bytes"))}',
                comparison=_comparisons(
                    ('files ' + _delta(deltas, 'total_files'))
                    if _delta(deltas, 'total_files') else '',
                    ('volume ' + _bytes_delta(deltas, 'total_bytes'))
                    if _bytes_delta(deltas, 'total_bytes') else ''),
                source=output_source),
        ])
        if not progress.get('target_completion_available'):
            notes.append(
                'PCS records establish managed Rucio placement, not '
                'production-target completion. '
                + str(progress.get('target_completion_reason') or '').rstrip('.')
                + '.')
        duplicates = int(progress.get('duplicate_output_records') or 0)
        if duplicates:
            patterns = progress.get('duplicate_status_patterns') or []
            expected_overlap = bool(patterns) and all(
                ':submitted' in str(item.get('task_representations') or '')
                and ':past_output' in str(item.get('task_representations') or '')
                for item in patterns)
            consistency = progress.get('duplicate_consistency') or {}
            agreement = (
                not consistency.get('file_count_conflicts')
                and not consistency.get('stage_conflicts')
                and not consistency.get('completion_conflicts'))
            notes.append(
                f'{duplicates:,} PCS output DIDs occur in multiple '
                + ('submitted-task and past-output rows. '
                   if expected_overlap else 'task rows. ')
                + ('File counts, stages, and placement states agree; '
                   if agreement else 'Some repeated records disagree; ')
                + 'totals use the most recently checked record for each DID.')
        for error in progress.get('source_errors') or []:
            problems.append(f'PCS progress source warning: {error}')
    else:
        problems.append('PCS campaign progress unavailable: '
                        + str(progress.get('reason') or 'unknown reason'))

    panda = _member(rollup, 'panda_health')
    if panda.get('available'):
        statuses = panda.get('task_statuses') or {}
        status_text = '; '.join(
            f'{status} {_number(count)}' for status, count in sorted(statuses.items()))
        jobs = panda.get('jobs') or {}
        comparison = ''
        if deltas and deltas.get('available'):
            comparison = _comparisons(
                ('tasks ' + _delta(deltas, 'panda_task_count'))
                if _delta(deltas, 'panda_task_count') else '',
                'finished ' + _delta(deltas, 'lifetime_jobs_finished'),
                'final failures '
                + _delta(deltas, 'lifetime_jobs_final_failed'))
        current.append(_row(
            'panda_lifetime_state',
            f'PanDA lifetime campaign state '
            f'({_number(panda.get("panda_task_count"))} tasks)',
            f'{_number(panda.get("panda_task_count"))} tasks'
            + (f' ({status_text})' if status_text else '')
            + f'; {_number(jobs.get("nfinished"))} finished jobs; '
              f'{_number(jobs.get("nfinalfailed"))} final failures',
            comparison=comparison,
            source='direct PanDA DB; PCS associations plus campaign-name discovery'))
    else:
        problems.append('PanDA lifetime state unavailable: '
                        + str(panda.get('reason') or 'unknown reason'))

    if dispositions.get('available'):
        counts = dispositions.get('dispositions') or {}
        current.append(_row(
            'disposition_state', 'Dataset dispositions',
            '; '.join(f'{state} {_number(count)}'
                      for state, count in sorted(counts.items())),
            comparison=_disposition_changes(deltas),
            source='current PCS state'))

    if system.get('available'):
        counts = system.get('counts') or {}
        current.append(_row(
            'platform_status', 'Production platform',
            f'{system.get("overall_status") or "unknown"}; '
            f'{_number(counts.get("ok"))} ok, '
            f'{_number(counts.get("warning"))} warning, '
            f'{_number(counts.get("error"))} error',
            source=f'system-status cache as of {_timestamp(system.get("latest_checked_at"))}'))
    else:
        problems.append('Production platform status unavailable.')

    credentials = _member(rollup, 'credential_status')
    if credentials.get('available'):
        current.append(_row(
            'credential_status', 'Automation credentials',
            credentials.get('outcome') or 'ok',
            comparison=credentials.get('reason') or '',
            source=f'credential check as of {_timestamp(credentials.get("checked_at"))}'))
    else:
        problems.append('Automation credential status unavailable: '
                        + str(credentials.get('reason') or 'no check record'))

    if not (deltas and deltas.get('available')):
        problems.append(str((deltas or {}).get('reason')
                            or 'No earlier production analytics snapshot is available for state comparisons.'))

    return {
        'schema': FACTS_SCHEMA,
        'campaign': rollup.get('campaign') or '',
        'evidence_window': {
            'start': window.get('start') or '',
            'end': window.get('end') or '',
            'display_et': _interval(window.get('start'), window.get('end')),
        },
        'generated_at': rollup.get('generated_at') or '',
        'activity': activity,
        'current_state': current,
        'evidence_notes': notes,
        'evidence_problems': problems,
    }


def _escape(value):
    return str(value or '').replace('|', '\\|').replace('\n', ' ')


def _table(rows):
    lines = [
        '| Fact | Value | Comparison | Evidence |',
        '|---|---:|---|---|',
    ]
    for row in rows:
        lines.append('| {fact} | {value} | {comparison} | {source} |'.format(
            **{key: _escape(row.get(key))
               for key in ('fact', 'value', 'comparison', 'source')}))
    return '\n'.join(lines)


def _fact_notes(facts):
    notes = list((facts or {}).get('evidence_notes') or [])
    if not notes:
        return ''
    return '**Accounting notes**\n\n' + '\n'.join(
        f'- {_escape(note)}' for note in notes)


def _comparison_span(deltas):
    hours = (deltas or {}).get('target_span_hours')
    if not isinstance(hours, (int, float)):
        return 'the reporting window'
    if hours % 24 == 0:
        days = hours / 24
        return f'{days:g} d'
    return f'{hours:g} h'


def _bullets(items, empty='No additional interpretation was required.'):
    clean = [str(item).strip() for item in items or [] if str(item).strip()]
    return '\n'.join(f'- {item}' for item in clean) if clean else empty


def _issues(items):
    if not items:
        return 'No issue requiring human action was identified.'
    lines = [
        '| Severity | Issue | Evidence | Action | Owner |',
        '|---|---|---|---|---|',
    ]
    for item in items:
        lines.append('| {severity} | {title} | {evidence} | {action} | {owner} |'.format(
            severity=_escape(item.get('severity')),
            title=_escape(item.get('title')),
            evidence=_escape('; '.join(item.get('evidence') or [])),
            action=_escape(item.get('action')),
            owner=_escape(item.get('owner')),
        ))
    return '\n'.join(lines)


def _software(items):
    if not items:
        return 'No material software or release change was established for the interval.'
    lines = ['| Finding | Evidence | Production significance |',
             '|---|---|---|']
    for item in items:
        lines.append('| {finding} | {evidence} | {significance} |'.format(
            finding=_escape(item.get('finding')),
            evidence=_escape('; '.join(item.get('evidence') or [])),
            significance=_escape(item.get('significance')),
        ))
    return '\n'.join(lines)


def _nested_markdown(content):
    """Keep a supplied narrative's headings subordinate to the bundle Page."""
    def replace(match):
        level = min(len(match.group(1)) + 3, 6)
        return '#' * level + ' '
    return re.sub(r'^(#{1,6})\s+', replace, str(content or ''),
                  flags=re.MULTILINE).strip()


def _manifest_table(entries):
    lines = [
        '| Source | Result | Latency | Request | Detail |',
        '|---|---|---:|---|---|',
    ]
    for entry in entries or []:
        url = str(entry.get('url') or '')
        request = f'[source]({url})' if url else ''
        detail = entry.get('detail') or entry.get('error') or ''
        latency = (f'{int(entry.get("ms"))} ms'
                   if entry.get('ms') is not None else '')
        lines.append('| {source} | {result} | {latency} | {request} | {detail} |'.format(
            source=_escape(entry.get('source')),
            result='available' if entry.get('ok') else 'failed',
            latency=_escape(latency), request=request, detail=_escape(detail)))
    return '\n'.join(lines)


def _member_table(rollup):
    lines = [
        '| Evidence member | Available | Computed (ET) | Window (ET) | Limitation |',
        '|---|---|---|---|---|',
    ]
    for name, block in sorted(((rollup or {}).get('members') or {}).items()):
        data = block.get('data') or {}
        window = block.get('window') or {}
        available = data.get('available')
        anchor = 'analytics-member-' + re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')
        lines.append('| {name} | {available} | {computed} | {window} | {reason} |'.format(
            name=f'[{_escape(name)}](#{anchor})',
            available=('yes' if available else 'no'),
            computed=_escape(_timestamp(block.get('computed_at'))),
            window=_escape(_interval(window.get('start'), window.get('end'))),
            reason=_escape(data.get('reason') or ''),
        ))
    return '\n'.join(lines)


def _display_value(value):
    if value is None:
        return ''
    if isinstance(value, bool):
        return 'yes' if value else 'no'
    if isinstance(value, float):
        return f'{value:g}'
    text = str(value)
    if text.startswith(('https://', 'http://')):
        return f'[{_escape(text)}]({text})'
    return _escape(text)


def _structured_markdown(value, heading_level=5):
    """Readable rendering for bounded raw evidence blocks."""
    if isinstance(value, dict):
        scalars = [(key, item) for key, item in value.items()
                   if not isinstance(item, (dict, list))]
        nested = [(key, item) for key, item in value.items()
                  if isinstance(item, (dict, list))]
        parts = []
        if scalars:
            lines = ['| Field | Value |', '|---|---|']
            lines.extend(
                f'| {_escape(key)} | {_display_value(item)} |'
                for key, item in scalars)
            parts.append('\n'.join(lines))
        for key, item in nested:
            heading = ('#' * min(heading_level, 6) + f' {_escape(key)}'
                       if heading_level <= 6 else f'**{_escape(key)}**')
            parts.extend([heading, _structured_markdown(item, heading_level + 1)])
        return '\n\n'.join(part for part in parts if part) or 'No data.'
    if isinstance(value, list):
        if not value:
            return 'None.'
        if all(not isinstance(item, (dict, list)) for item in value):
            return '\n'.join(f'- {_display_value(item)}' for item in value)
        if all(isinstance(item, dict) for item in value):
            keys = []
            for item in value:
                for key in item:
                    if key not in keys:
                        keys.append(key)
            if all(not isinstance(item.get(key), (dict, list))
                   for item in value for key in keys):
                lines = [
                    '| ' + ' | '.join(_escape(key) for key in keys) + ' |',
                    '|' + '|'.join('---' for _ in keys) + '|',
                ]
                for item in value:
                    lines.append('| ' + ' | '.join(
                        _display_value(item.get(key)) for key in keys) + ' |')
                return '\n'.join(lines)
        parts = []
        for index, item in enumerate(value, 1):
            parts.extend([
                f'**Item {index}**',
                _structured_markdown(item, heading_level),
            ])
        return '\n\n'.join(parts)
    return _display_value(value)


def _delta_table(deltas):
    if not (deltas or {}).get('available'):
        return str((deltas or {}).get('reason') or 'No comparison is available.')
    labels = (
        ('task_count', 'PCS task rows', _number),
        ('tasks_with_processing', 'PCS tasks with processing', _number),
        ('outputs_total', 'Unique outputs', _number),
        ('outputs_placement_complete', 'Fully placed outputs', _number),
        ('total_files', 'Recorded output files', _number),
        ('total_bytes', 'Recorded output volume', _bytes),
        ('panda_task_count', 'PanDA tasks', _number),
        ('lifetime_jobs_finished', 'Lifetime finished jobs', _number),
        ('lifetime_jobs_final_failed', 'Lifetime final-failed jobs', _number),
    )
    lines = [
        '| State | Earlier | Current | Change |',
        '|---|---:|---:|---:|',
    ]
    for key, label, formatter in labels:
        item = deltas.get(key)
        if not isinstance(item, dict):
            continue
        change = int(item.get('delta') or 0)
        formatted_change = (
            f'{"+" if change >= 0 else "-"}{formatter(abs(change))}'
            if formatter is _bytes else f'{change:+,}')
        lines.append(
            f'| {label} | {formatter(item.get("previous"))} | '
            f'{formatter(item.get("current"))} | {formatted_change} |')
    return '\n'.join(lines)


def render_bundle_page(bundle):
    """Render a complete evidence bundle for fast human review and audit."""
    params = bundle.get('params') or {}
    campaign = params.get('campaign') or ''
    kind = str(params.get('kind') or '').capitalize()
    facts = bundle.get('facts') or {}
    rollup = bundle.get('rollup') or {}
    deltas = bundle.get('deltas') or {}
    floor = rollup.get('floor') or {}
    narratives = bundle.get('narratives') or {}

    metadata = (
        f'**Generated:** {_timestamp(bundle.get("generated_at"))} · '
        f'**Evidence window:** '
        f'{facts.get("evidence_window", {}).get("display_et") or "unknown"} · '
        f'**Schema:** `{bundle.get("schema") or "unknown"}` · '
        f'**Fetch status:** {"degraded" if bundle.get("degraded") else "complete"}'
    )
    reasons = floor.get('reasons') or []
    floor_text = f'**{str(floor.get("verdict") or "unknown").capitalize()}**'
    if reasons:
        floor_text += '\n\n' + '\n'.join(f'- {_escape(reason)}' for reason in reasons)
    else:
        floor_text += ' — no mechanical threshold was crossed.'

    comparison_meta = (
        f'**Requested baseline:** {_timestamp(deltas.get("target_generated_at"))} · '
        f'**Selected snapshot:** {_timestamp(deltas.get("baseline_generated_at"))} · '
        f'**Actual elapsed interval:** {deltas.get("elapsed_hours")} hours · '
        f'**Distance from requested baseline:** '
        f'{deltas.get("baseline_distance_hours")} hours'
        if deltas.get('available') else
        str(deltas.get('reason') or 'No comparison is available.')
    )

    sections = [
        f'# ePIC Campaign {campaign} — {kind} Assessment Evidence Bundle',
        metadata,
        'This is the complete production evidence supplied to the assessor, '
        'rendered deterministically as a human review document.',
        '<a id="bundle-metadata"></a>', '### Bundle metadata',
        _structured_markdown({
            'schema': bundle.get('schema'),
            'generated_at': bundle.get('generated_at'),
            'parameters': params,
            'degraded': bundle.get('degraded'),
            'degraded_meaning': bundle.get('degraded_meaning'),
            'prior_ai_reports_supplied': bundle.get('prior_ai_reports_supplied'),
        }),
        '<a id="production-facts"></a>', '### Production facts',
        '#### Interval activity', _table(facts.get('activity') or []),
        '#### Current state', _table(facts.get('current_state') or []),
        '<a id="mechanical-verdict-floor"></a>',
        '### Mechanical verdict floor', floor_text,
        '#### Floor context', _structured_markdown(
            floor.get('standing_context') or {}),
        '<a id="state-comparison"></a>',
        '### State comparison', comparison_meta, _delta_table(deltas),
        '#### Complete comparison record', _structured_markdown(deltas),
        '<a id="rollup-identity"></a>', '### Campaign rollup identity',
        _structured_markdown({
            key: value for key, value in rollup.items()
            if key not in ('members', 'floor')
        }),
        '<a id="evidence-provenance"></a>',
        '### Evidence quality and provenance',
        '<a id="acquisition-manifest"></a>',
        '#### Acquisition manifest', _manifest_table(bundle.get('manifest') or []),
        '#### Analytics member index', _member_table(rollup),
    ]
    notes = facts.get('evidence_notes') or []
    if notes:
        sections.extend([
            '#### Evidence notes',
            '\n'.join(f'- {_escape(note)}' for note in notes),
        ])
    problems = facts.get('evidence_problems') or []
    if problems:
        sections.extend([
            '#### Evidence problems and limitations',
            '\n'.join(f'- {_escape(problem)}' for problem in problems),
        ])

    for label, heading in (
            ('campaign', 'Campaign narrative'),
            ('general', 'General production context')):
        narrative = narratives.get(label) or {}
        name = narrative.get('name') or 'unavailable'
        version = narrative.get('version') or 0
        content = _nested_markdown(narrative.get('content') or '')
        sections.extend([
            f'<a id="{label}-narrative"></a>', f'### {heading}',
            f'**Source:** `{name}` version {version} · '
            f'**Page group:** `{narrative.get("group_id") or ""}`',
            content or 'No narrative content was available.',
        ])

    sections.extend([
        '<a id="analytics-evidence"></a>', '### Analytics evidence',
        'Each member below is the complete structured evidence block '
        'used to construct the report facts.',
    ])
    for name, block in sorted((rollup.get('members') or {}).items()):
        anchor = 'analytics-member-' + re.sub(
            r'[^a-z0-9]+', '-', name.lower()).strip('-')
        window = block.get('window') or {}
        sections.extend([
            f'<a id="{anchor}"></a>', f'#### `{name}`',
            f'**Schema version:** {block.get("schema_version") or ""} · '
            f'**Computed:** {_timestamp(block.get("computed_at"))} · '
            f'**Window:** {_interval(window.get("start"), window.get("end"))}',
            _structured_markdown(block.get('data') or {}),
        ])

    return '\n\n'.join(part for part in sections if part).strip()


def _raw_fence(value, language='text'):
    """Fence unmodified run evidence without colliding with its contents."""
    text = str(value or '')
    longest = max((len(match.group(0)) for match in re.finditer(r'`+', text)),
                  default=0)
    fence = '`' * max(3, longest + 1)
    return f'{fence}{language}\n{text}\n{fence}'


def render_investigation_page(bundle, artifact, *, job_id, model_output,
                              run_log):
    """Render the post-run evidence used to audit model-derived findings."""
    params = bundle.get('params') or {}
    campaign = params.get('campaign') or ''
    kind = str(params.get('kind') or '').capitalize()
    bundle_artifact = bundle.get('artifact') or {}
    bundle_url = bundle_artifact.get('url') or ''
    generation = artifact.get('generation') or {}
    records = generation.get('investigation') or []
    sections = [
        f'# ePIC Campaign {campaign} — {kind} Assessor Investigation Evidence',
        f'**Job:** `{job_id}` · **Input bundle:** '
        + (f'[evidence bundle]({bundle_url})' if bundle_url else 'unavailable'),
        'This one-off Page preserves the assessor-supplied live-evidence '
        'records, its exact contract artifact, and the runner transcript. '
        'It is not an assessment and is not used as production evidence by '
        'later reports.',
        '### Live investigation records',
    ]
    if records:
        for index, record in enumerate(records, 1):
            sections.extend([
                f'#### {index}. {_escape(record.get("claim"))}',
                f'**Source:** `{_escape(record.get("source"))}`',
                '##### Request',
                _structured_markdown(record.get('request') or {}),
                '##### Supporting result fields',
                _structured_markdown(record.get('result') or {}),
            ])
    else:
        sections.append(
            'No live tool result was used in a concrete model-derived claim.')
    sections.extend([
        '### Exact model artifact',
        _raw_fence(model_output, 'markdown'),
        '### Runner execution transcript',
        (_raw_fence((run_log or {}).get('stderr'), 'text')
         if (run_log or {}).get('stderr') else
         'No runner execution transcript was returned by corun.'),
    ])
    thinking = (run_log or {}).get('thinking')
    if thinking:
        sections.extend(['### Runner thinking trace', _raw_fence(thinking, 'text')])
    error = (run_log or {}).get('error')
    if error:
        sections.extend(['### Runner error', _raw_fence(error, 'text')])
    return '\n\n'.join(part for part in sections if part).strip()


def _generation(bundle, artifact):
    generation = artifact.get('generation') or {}
    consulted = generation.get('consulted') or []
    bundle_url = (bundle.get('artifact') or {}).get('url') or ''
    facts_schema = (bundle.get('facts') or {}).get('schema') or 'bundle facts'
    assembly = (
        f'- Production facts and comparisons were rendered procedurally from '
        f'[{_escape(facts_schema)}]({bundle_url}#production-facts); '
        'the assessment, issues, '
        'software findings, and outlook came from the contract-validated '
        'model artifact.'
        if bundle_url else
        '- Production facts and comparisons were rendered procedurally from '
        'the evidence bundle; judgment came from the contract-validated '
        'model artifact.'
    )
    blocks = [f'**Assembly**\n\n{assembly}']
    investigation = bundle.get('investigation_artifact') or {}
    if investigation.get('url'):
        blocks.append(
            '**Investigation evidence**\n\n'
            f'- [Live evidence records, exact model artifact, and runner '
            f'transcript]({investigation["url"]}).')
    if bundle_url:
        blocks.append(
            '**Bundle artifacts**\n\n'
            f'- Inlined here: [production facts]({bundle_url}#production-facts) '
            f'and [state comparison]({bundle_url}#state-comparison).\n'
            f'- Linked for review: [campaign narrative]({bundle_url}#campaign-narrative), '
            f'[general context]({bundle_url}#general-narrative), '
            f'[source manifest]({bundle_url}#acquisition-manifest), '
            f'and [analytics evidence]({bundle_url}#analytics-evidence).')
    if consulted:
        blocks.append('**Consulted**\n\n' + '\n'.join(
            f'- {_escape(item.get("source"))}: {_escape(item.get("contribution"))}'
            for item in consulted))

    problems = list(generation.get('problems') or [])
    unavailable = list(generation.get('unavailable') or [])
    for entry in bundle.get('manifest') or []:
        if not entry.get('ok'):
            problems.append(
                f'{entry.get("source")}: {entry.get("error") or "fetch failed"}')
    problems.extend((bundle.get('facts') or {}).get('evidence_problems') or [])

    if problems:
        problem_text = '\n'.join(
            f'- {_escape(item)}' for item in dict.fromkeys(problems))
    else:
        problem_text = '- None reported.'
    blocks.append(f'**Problems and limitations**\n\n{problem_text}')
    if unavailable:
        blocks.append('**Unavailable**\n\n' + '\n'.join(
            f'- {_escape(item)}' for item in dict.fromkeys(unavailable)))
    return '\n\n'.join(blocks)


def report_title(campaign, kind, date):
    label = 'Daily Report' if kind == 'daily' else 'Weekly Summary'
    return f'ePIC Production Campaign {campaign} — {label}, {date}'


def render_report(bundle, artifact, kind):
    """Assemble the published report from facts plus bounded judgment."""
    params = bundle.get('params') or {}
    campaign = params.get('campaign') or ''
    generated = datetime.fromisoformat(
        str(bundle.get('generated_at')).replace('Z', '+00:00')).astimezone(ET)
    title = report_title(campaign, kind, generated.date().isoformat())
    facts = bundle.get('facts') or {}
    verdict = str(artifact.get('verdict') or '').capitalize()
    metadata = (f'**Verdict:** {verdict} · **Evidence window:** '
                f'{facts.get("evidence_window", {}).get("display_et") or "unknown"}')
    bundle_url = ((bundle.get('artifact') or {}).get('url') or '')
    investigation_url = (
        (bundle.get('investigation_artifact') or {}).get('url') or '')
    if bundle_url:
        metadata += f' · [Evidence bundle]({bundle_url})'
        artifact_nav = (
            f'**Evidence artifacts:** '
            f'[facts (inlined)]({bundle_url}#production-facts) · '
            f'[campaign narrative]({bundle_url}#campaign-narrative) · '
            f'[general context]({bundle_url}#general-narrative) · '
            f'[source manifest]({bundle_url}#acquisition-manifest) · '
            f'[analytics members]({bundle_url}#analytics-evidence)')
        if investigation_url:
            artifact_nav += (
                f' · [investigation evidence]({investigation_url})')
    else:
        artifact_nav = ''
    deltas = bundle.get('deltas') or {}
    comparison = ''
    if deltas.get('available'):
        comparison = (
            f'**State comparison:** {_timestamp(deltas.get("baseline_generated_at"))} '
            f'({deltas.get("elapsed_hours")} h elapsed; requested '
            f'{_comparison_span(deltas)} baseline; selected snapshot '
            f'{deltas.get("baseline_distance_hours")} h from target)')

    if kind == 'weekly':
        sections = [
            f'# {title}', metadata, artifact_nav, comparison,
            '### Executive assessment', _bullets(artifact.get('assessment')),
            '### Campaign state', _table(facts.get('current_state') or []),
            _fact_notes(facts),
            '### Production this week', _table(facts.get('activity') or []),
            _bullets(artifact.get('activity_interpretation')),
            '### Software and release state', _software(artifact.get('software_findings')),
            '### Issues and responsibilities', _issues(artifact.get('top_issues')),
            '### Outlook', _bullets(artifact.get('outlook'),
                                     empty='No evidence-grounded change to the near-term outlook was identified.'),
            '### Generation report', _generation(bundle, artifact),
        ]
    else:
        interpretation = list(artifact.get('assessment') or [])
        interpretation.extend(artifact.get('activity_interpretation') or [])
        sections = [
            f'# {title}', metadata, artifact_nav, comparison,
            '### Production facts', '#### Interval activity',
            _table(facts.get('activity') or []),
            '#### Current state', _table(facts.get('current_state') or []),
            _fact_notes(facts),
            '### Operational assessment', _bullets(interpretation),
        ]
        if artifact.get('software_findings'):
            sections.extend([
                '### Software and release state',
                _software(artifact.get('software_findings')),
            ])
        sections.extend([
            '### Issues and follow-up', _issues(artifact.get('top_issues')),
        ])
        if artifact.get('outlook'):
            sections.extend(['### Outlook', _bullets(artifact.get('outlook'))])
        sections.extend([
            '### Generation report', _generation(bundle, artifact),
        ])
    sections = [part for part in sections if part]
    return '\n\n'.join(sections).strip()
