"""Assessment enforcement — the harness end on the completion callback
(docs/EPICPROD_ASSESSMENTS_V1.md).

Runs under the deployed venv with Django (``python -m
swf_epicprod.assessment.enforce --job-id ... --prompt-group-id ...
--page-group-id ... --status ...``), dispatched by the prod-ops agent's
``assessment_completed`` handler. It validates the model's artifact
against the schema, enforces the verdict floor (raise-only), retries a
malformed run once, quarantines a second failure (raw output retained,
never dropped), and registers the assessment — every scheduled slot resolves
to a visible outcome. A run that fails before returning a result (timeout,
crash) is resubmitted once and delivers a midstream-salvage report built
from the runner transcript, with the transcript preserved as a crashed-run
evidence page.
"""

import argparse
import json
import logging
import os
import sys
from pathlib import Path

# The agent dispatches this module with cwd at the deploy release; prefer
# the release's Django project over the copied venv's editable dev path.
_release_src = Path.cwd() / 'src'
if (_release_src / 'swf_monitor_project').exists():
    sys.path.insert(0, str(_release_src))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402

django.setup()

from monitor_app.epicprod_logging import log_epicprod_action  # noqa: E402
from monitor_app.mcp.ai_content import _register_ai_assessment_sync  # noqa: E402
from swf_epicprod.assessment import reporting, spec  # noqa: E402
from swf_epicprod.assessment.bundle import _get  # noqa: E402

CORUN_API_URL = (os.environ.get('CORUN_API_URL', '').rstrip('/')
                 or (os.environ.get('CORUN_BASE_URL', '').rstrip('/') + '/api/v1'
                     if os.environ.get('CORUN_BASE_URL') else ''))
CORUN_API_TOKEN = os.environ.get('CORUN_API_TOKEN', '')
CORUN_ASSESSMENT_SECTION = os.environ.get(
    'CORUN_ASSESSMENT_SECTION', spec.DEFAULT_SECTION)
CORUN_ASSESSMENT_BUNDLE_SECTION = os.environ.get(
    'CORUN_ASSESSMENT_BUNDLE_SECTION', spec.DEFAULT_BUNDLE_SECTION)
CORUN_WEB_URL = os.environ.get('CORUN_WEB_URL', '').rstrip('/')
if not CORUN_WEB_URL and CORUN_API_URL.endswith('/api/v1'):
    CORUN_WEB_URL = CORUN_API_URL[:-len('/api/v1')]


def _definition_for(kind):
    # DAILY was NIGHTLY until 2026-07-12; honor an un-migrated environment.
    legacy = 'NIGHTLY' if kind == 'daily' else ''
    return (os.environ.get(f'CORUN_ASSESSMENT_DEFINITION_{kind.upper()}')
            or (os.environ.get(f'CORUN_ASSESSMENT_DEFINITION_{legacy}', '')
                if legacy else '')
            or os.environ.get('CORUN_ASSESSMENT_DEFINITION', ''))

log = logging.getLogger('assessment_enforce')


def _log(action, *, outcome, subject_key='', reason='', sublevel='normal', **counts):
    log_epicprod_action(
        'assessment-harness', action,
        subject_type='campaign' if subject_key else '',
        subject_key=subject_key, outcome=outcome, reason=reason,
        sublevel=sublevel, live_default=outcome != 'ok', **counts)


def _post(url, payload):
    import urllib.request
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={'Content-Type': 'application/json',
                 'Accept': 'application/json',
                 'Authorization': f'Token {CORUN_API_TOKEN}'})
    import urllib.error
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode() or '{}')


def _persist_investigation_evidence(bundle, artifact, *, job_id,
                                    page_group_id, model_output):
    """Store one directly linked, hidden post-run evidence Page."""
    run_log = {}
    try:
        run_log = _get(f'{CORUN_API_URL}/jobs/{job_id}/log/',
                       token=CORUN_API_TOKEN)
    except Exception as e:
        artifact['generation']['problems'].append(
            f'corun runner transcript fetch failed: {e}')
    params = bundle.get('params') or {}
    campaign = str(params.get('campaign') or '')
    kind = str(params.get('kind') or '')
    content = reporting.render_investigation_page(
        bundle, artifact, job_id=job_id, model_output=model_output,
        run_log=run_log)
    page = _post(
        f'{CORUN_API_URL}/pages/',
        {
            'section': CORUN_ASSESSMENT_BUNDLE_SECTION,
            'title': (
                f'ePIC {campaign} {kind} assessor investigation evidence — '
                f'{bundle.get("generated_at") or ""}'),
            'content': content,
            'data': {
                'ui_visible': False,
                'artifact_type': 'campaign_assessment_investigation_evidence',
                'source_system': 'epicprod',
                'subject_type': 'campaign',
                'subject_key': campaign,
                'campaign': campaign,
                'assessment_kind': kind,
                'kind': kind,
                'schema_version': spec.SCHEMA_VERSION,
                'evidence_generated_at': bundle.get('generated_at') or '',
                'input_bundle_id': str(
                    (bundle.get('artifact') or {}).get('id') or ''),
                'job_id': job_id,
                'model_result_page_id': page_group_id,
            },
            'tags': [
                'investigation-evidence', 'epicprod',
                f'campaign:{campaign}', f'assessment:{kind}',
            ],
        })
    evidence_id = str(page.get('group_id') or '')
    if not evidence_id:
        raise RuntimeError(
            'corun investigation Page response contained no group id')
    artifact_ref = {
        'type': 'corun_page',
        'id': evidence_id,
        'url': f'{CORUN_WEB_URL}/page/{evidence_id}/',
        'section': CORUN_ASSESSMENT_BUNDLE_SECTION,
    }
    bundle['investigation_artifact'] = artifact_ref
    return artifact_ref



def _report_title(prose, slot):
    """First markdown heading, the same convention corun's worker uses.
    Markdown links are flattened to their text — the title renders in
    plain-text surfaces (page header lines, registrations)."""
    import re
    for line in (prose or '').splitlines():
        s = line.strip()
        if s.startswith('#'):
            t = s.lstrip('#').strip()
            t = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', t).strip()
            if t:
                return t[:200]
    return f'Campaign assessment {slot}'

def _already_rerun(prompt_group_id):
    """A resubmission is recorded in the action stream; its presence bounds
    the failed-run path to one automatic rerun — same pattern as
    _already_retried for malformed artifacts."""
    from monitor_app.models import AppLog
    return AppLog.objects.filter(
        app_name='epicprod',
        extra_data__action='assessment_rerun',
        extra_data__prompt_group_id=str(prompt_group_id)).exists()


def _persist_crashed_run_evidence(bundle, *, job_id, run_log):
    """Store the crashed run's transcript as a hidden corun evidence Page."""
    params = bundle.get('params') or {}
    campaign = str(params.get('campaign') or '')
    kind = str(params.get('kind') or '')
    content = reporting.render_crashed_run_page(
        bundle, job_id=job_id, run_log=run_log)
    page = _post(
        f'{CORUN_API_URL}/pages/',
        {
            'section': CORUN_ASSESSMENT_BUNDLE_SECTION,
            'title': (
                f'ePIC {campaign} {kind} assessment crashed-run evidence — '
                f'{bundle.get("generated_at") or ""}'),
            'content': content,
            'data': {
                'ui_visible': False,
                'artifact_type': 'campaign_assessment_crashed_run_evidence',
                'source_system': 'epicprod',
                'subject_type': 'campaign',
                'subject_key': campaign,
                'campaign': campaign,
                'assessment_kind': kind,
                'kind': kind,
                'schema_version': spec.SCHEMA_VERSION,
                'evidence_generated_at': bundle.get('generated_at') or '',
                'input_bundle_id': str(
                    (bundle.get('artifact') or {}).get('id') or ''),
                'job_id': job_id,
            },
            'tags': [
                'crashed-run-evidence', 'epicprod',
                f'campaign:{campaign}', f'assessment:{kind}',
            ],
        })
    evidence_id = str(page.get('group_id') or '')
    if not evidence_id:
        raise RuntimeError(
            'corun crashed-run Page response contained no group id')
    return {
        'type': 'corun_page',
        'id': evidence_id,
        'url': f'{CORUN_WEB_URL}/page/{evidence_id}/',
        'section': CORUN_ASSESSMENT_BUNDLE_SECTION,
    }


def _handle_failed_run(args, *, bundle, campaign, kind, slot, floor,
                       floor_verdict, elapsed_s):
    """The failed-run path (docs/EPICPROD_ASSESSMENTS.md, Harness
    Lifecycle): a run that dies before submitting its artifact still
    delivers. Persist the transcript as crashed-run evidence, resubmit the
    same prompt once, and register a midstream-salvage report — floor
    verdict, salvaged narration, explicit not-a-completed-assessment
    header — so the slot fills and the freshness check clears."""
    run_log = {}
    try:
        run_log = _get(f'{CORUN_API_URL}/jobs/{args.job_id}/log/',
                       token=CORUN_API_TOKEN)
    except Exception as e:
        log.warning('job log fetch failed: %s', e)
    error = str(run_log.get('error') or '')

    crash_evidence = {}
    try:
        crash_evidence = _persist_crashed_run_evidence(
            bundle, job_id=args.job_id, run_log=run_log)
    except Exception as e:
        log.warning('crashed-run evidence persistence failed: %s', e)

    rerun_job_id = ''
    rerun_exhausted = _already_rerun(args.prompt_group_id)
    if not rerun_exhausted:
        try:
            job = _post(f'{CORUN_API_URL}/jobs/',
                        {'prompt_group_id': args.prompt_group_id,
                         'definition_id': _definition_for(kind)})
            rerun_job_id = str(job.get('id') or job.get('job_id') or '')
            _log('assessment_rerun', outcome='ok', subject_key=campaign,
                 slot=slot, prompt_group_id=args.prompt_group_id,
                 rerun_job_id=rerun_job_id,
                 reason=f'run {args.status}: {error}')
        except Exception as e:
            log.warning('resubmission failed: %s', e)
            _log('assessment_rerun', outcome='error', subject_key=campaign,
                 slot=slot, prompt_group_id=args.prompt_group_id,
                 reason=f'resubmission failed: {e}')

    salvage_registered = False
    try:
        salvage = reporting.render_salvage_report(
            bundle, kind, run_log=run_log, crash_evidence=crash_evidence,
            rerun_job_id=rerun_job_id, rerun_exhausted=rerun_exhausted)
        result = _register_ai_assessment_sync(
            subject_type='campaign', subject_key=campaign,
            assessment=salvage,
            username='assessment-harness', ai='corun-job',
            subject_label='', subject_url='',
            title=_report_title(salvage, slot),
            data={'assessment_kind': kind, 'origin': 'scheduled',
                  'schema_version': spec.SCHEMA_VERSION, 'slot': slot,
                  'verdict': floor_verdict, 'salvaged': True,
                  'run_failed': error or f'run {args.status}',
                  'floor': floor,
                  'generation_harness': {
                      'manifest': bundle.get('manifest'),
                      'degraded': bundle.get('degraded'),
                      'bundle': bundle.get('artifact'),
                      'elapsed_s': elapsed_s,
                      'job_id': args.job_id,
                      'crash_evidence': crash_evidence,
                      'rerun_job_id': rerun_job_id,
                      'enforcement': 'salvaged'}})
        salvage_registered = bool(result.get('success'))
        if not salvage_registered:
            log.warning('salvage registration failed: %s',
                        result.get('error'))
    except Exception as e:
        log.warning('salvage report failed: %s', e)

    _log('assessment_enforce', outcome='error', subject_key=campaign,
         sublevel='high', slot=slot, job_id=args.job_id,
         salvaged=salvage_registered, rerun_job_id=rerun_job_id,
         crash_evidence_page_id=str(crash_evidence.get('id') or ''),
         reason=f'run {args.status}: {error}')
    return 0


def _already_retried(prompt_group_id):
    """A retry is recorded in the action stream; its presence makes this
    attempt 2 — no state model needed."""
    from monitor_app.models import AppLog
    return AppLog.objects.filter(
        app_name='epicprod',
        extra_data__action='assessment_retry',
        extra_data__prompt_group_id=str(prompt_group_id)).exists()


def _verdict_standing(campaign, kind, verdict):
    """How long this verdict has been standing: consecutive prior
    registrations of this campaign+kind at the same verdict. Mechanical,
    from the production-owned registration series — prior AI content is
    never read, only the recorded verdicts."""
    from monitor_app.models import AppLog
    kinds = ['daily', 'nightly'] if kind == 'daily' else [kind]
    rows = list(AppLog.objects.filter(
        app_name='epicprod',
        extra_data__action='assessment_register',
        extra_data__subject_key=str(campaign),
        extra_data__assessment_kind__in=kinds)
        .exclude(extra_data__quarantined=True)
        .order_by('-timestamp')
        .values_list('extra_data__verdict', 'timestamp')[:60])
    prior_consecutive = 0
    since = None
    for row_verdict, stamp in rows:
        if str(row_verdict or '') != str(verdict or ''):
            break
        prior_consecutive += 1
        since = stamp
    return {
        'prior_consecutive': prior_consecutive,
        'since': since.date().isoformat() if since else '',
        'previous_verdict': str(rows[0][0] or '') if rows else '',
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--job-id', required=True)
    parser.add_argument('--prompt-group-id', required=True)
    parser.add_argument('--page-group-id', default='')
    parser.add_argument('--status', required=True)
    parser.add_argument('--timing', default='',
                        help='model-run elapsed seconds from the callback')
    args = parser.parse_args()
    try:
        elapsed_s = round(float(args.timing), 1) if args.timing else None
    except ValueError:
        elapsed_s = None

    # The submitted bundle: floor, params, manifest — the harness's half
    # of the run's truth.
    prompt = _get(f'{CORUN_API_URL}/prompts/{args.prompt_group_id}/',
                  token=CORUN_API_TOKEN)
    try:
        submitted = json.loads(prompt.get('content') or '{}')
    except json.JSONDecodeError:
        submitted = {}
    bundle = submitted.get('bundle') or {}
    params = bundle.get('params') or {}
    campaign = params.get('campaign') or 'unknown'
    kind = params.get('kind') or 'daily'
    slot = submitted.get('slot') or ''
    floor = (((bundle.get('rollup') or {}).get('floor')) or {})
    floor_verdict = floor.get('verdict') or 'ok'

    if args.status != 'completed':
        return _handle_failed_run(
            args, bundle=bundle, campaign=campaign, kind=kind, slot=slot,
            floor=floor, floor_verdict=floor_verdict, elapsed_s=elapsed_s)

    page = _get(f'{CORUN_API_URL}/pages/{args.page_group_id}/',
                token=CORUN_API_TOKEN)
    content = page.get('content') or ''
    artifact, remainder, problems = spec.extract_artifact(content)
    if artifact is not None:
        problems += spec.validate_artifact(artifact)
        problems += spec.validate_remainder(remainder)
        problems += spec.validate_bundle_citations(artifact, bundle)

    if problems:
        is_repair = bool(submitted.get('repair'))
        if not is_repair and not _already_retried(args.prompt_group_id):
            repair_submission = dict(submitted)
            repair_submission['repair'] = {
                'validation_problems': problems,
                'previous_output': content,
                'instruction': (
                    'Produce a complete replacement JSON artifact. Correct every '
                    'listed validation problem while preserving supported '
                    'production finding.'),
            }
            retry_prompt = _post(
                f'{CORUN_API_URL}/prompts/',
                {'section': CORUN_ASSESSMENT_SECTION,
                 'content': json.dumps(repair_submission),
                 'definition_id': _definition_for(kind)})
            retry_prompt_group_id = str(
                retry_prompt.get('group_id') or '')
            job = _post(f'{CORUN_API_URL}/jobs/',
                        {'prompt_group_id': retry_prompt_group_id,
                         'definition_id': _definition_for(kind)})
            _log('assessment_retry', outcome='ok', subject_key=campaign,
                 slot=slot, prompt_group_id=args.prompt_group_id,
                 retry_prompt_group_id=retry_prompt_group_id,
                 retry_job_id=str(job.get('id') or ''),
                 reason='; '.join(problems))
            return 0
        # Second failure: quarantine — raw output retained and never rendered
        # as a valid report; verdict pinned to the mechanical floor.
        result = _register_ai_assessment_sync(
            subject_type='campaign', subject_key=campaign,
            assessment=content or '(empty model output)',
            username='assessment-harness', ai='corun-job',
            subject_label='', subject_url='',
            title=f'Quarantined assessment — {slot}',
            data={'assessment_kind': kind, 'origin': 'scheduled',
                  'schema_version': spec.SCHEMA_VERSION, 'slot': slot,
                  'verdict': floor_verdict, 'quarantined': True,
                  'problems': problems,
                  'generation_harness': {
                      'manifest': bundle.get('manifest'),
                      'degraded': bundle.get('degraded'),
                      'bundle': bundle.get('artifact'),
                      'elapsed_s': elapsed_s,
                      'job_id': args.job_id, 'enforcement': 'quarantined'}})
        _log('assessment_enforce', outcome='error', subject_key=campaign,
             sublevel='high', slot=slot, quarantined=True,
             corun_page_group_id=result.get('corun_page_group_id') or '',
             reason='quarantined after retry: ' + '; '.join(problems))
        return 0

    verdict = artifact.get('verdict')
    model_verdict = verdict
    floor_enforced = False
    if not spec.verdict_at_least(verdict, floor_verdict):
        floor_enforced = True
        artifact['verdict'] = verdict = floor_verdict

    narration = str(artifact.get('narration') or '').strip()
    investigation_artifact = {}
    try:
        investigation_artifact = _persist_investigation_evidence(
            bundle, artifact, job_id=args.job_id,
            page_group_id=args.page_group_id, model_output=content)
    except Exception as e:
        log.warning('investigation evidence persistence failed: %s', e)
        artifact['generation']['problems'].append(
            f'investigation evidence persistence failed: {e}')
    standing = {}
    try:
        standing = _verdict_standing(campaign, kind, verdict)
    except Exception as e:
        log.warning('verdict standing lookup failed: %s', e)
    harness_health = None
    if kind == 'weekly':
        try:
            from swf_epicprod.assessment.freshness import (
                harness_problem_aggregation)
            harness_health = harness_problem_aggregation(days=7)
        except Exception as e:
            log.warning('harness health aggregation failed: %s', e)
    report = reporting.render_report(bundle, artifact, kind,
                                     standing=standing,
                                     harness_health=harness_health)
    result = _register_ai_assessment_sync(
        subject_type='campaign', subject_key=campaign,
        assessment=report,
        username='assessment-harness', ai='corun-job',
        subject_label='', subject_url='',
        title=_report_title(report, slot),
        data={'assessment_kind': kind, 'origin': 'scheduled',
              'schema_version': spec.SCHEMA_VERSION, 'slot': slot,
              'verdict': verdict, 'narration': narration,
              'verdict_standing': standing,
              'model_verdict': model_verdict,
              'floor': floor, 'floor_enforced': floor_enforced,
              'structured': artifact,
              'prompt_group_id': args.prompt_group_id,
              'job_id': args.job_id,
              'generation_harness': {
                  'manifest': bundle.get('manifest'),
                  'degraded': bundle.get('degraded'),
                  'elapsed_s': elapsed_s,
                  'bundle_generated_at': bundle.get('generated_at'),
                  'bundle': bundle.get('artifact'),
                  'investigation': investigation_artifact,
                  'floor_enforced': floor_enforced}})
    if not result.get('success'):
        _log('assessment_enforce', outcome='error', subject_key=campaign,
             sublevel='high', slot=slot,
             reason=f"registration failed: {result.get('error')}")
        return 1
    problems = reporting._collect_problems(bundle, artifact)
    _log('assessment_enforce', outcome='ok', subject_key=campaign, slot=slot,
         verdict=verdict, floor_enforced=floor_enforced,
         degraded=bool(bundle.get('degraded')),
         problems=[p[:300] for p in problems[:20]],
         problems_count=len(problems),
         corun_page_group_id=result.get('corun_page_group_id') or '')
    print(f'{campaign} {slot}: registered verdict={verdict}'
          f'{" (floor-enforced)" if floor_enforced else ""}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
