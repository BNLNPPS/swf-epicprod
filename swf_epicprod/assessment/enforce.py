"""Assessment enforcement — the harness end on the completion callback
(docs/EPICPROD_ASSESSMENTS_V1.md).

Runs under the deployed venv with Django (``python -m
swf_epicprod.assessment.enforce --job-id ... --prompt-group-id ...
--page-group-id ... --status ...``), dispatched by the prod-ops agent's
``assessment_completed`` handler. It validates the model's artifact
against the schema, enforces the verdict floor (raise-only), retries a
malformed run once, quarantines a second failure (raw output retained,
never dropped), and registers the assessment — every scheduled slot resolves
to a visible outcome.
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
from swf_epicprod.assessment import spec  # noqa: E402
from swf_epicprod.assessment.bundle import _get  # noqa: E402

CORUN_API_URL = (os.environ.get('CORUN_API_URL', '').rstrip('/')
                 or (os.environ.get('CORUN_BASE_URL', '').rstrip('/') + '/api/v1'
                     if os.environ.get('CORUN_BASE_URL') else ''))
CORUN_API_TOKEN = os.environ.get('CORUN_API_TOKEN', '')
CORUN_ASSESSMENT_SECTION = os.environ.get(
    'CORUN_ASSESSMENT_SECTION', spec.DEFAULT_SECTION)


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

def _already_retried(prompt_group_id):
    """A retry is recorded in the action stream; its presence makes this
    attempt 2 — no state model needed."""
    from monitor_app.models import AppLog
    return AppLog.objects.filter(
        app_name='epicprod',
        extra_data__action='assessment_retry',
        extra_data__prompt_group_id=str(prompt_group_id)).exists()


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
        run_log = {}
        try:
            run_log = _get(f'{CORUN_API_URL}/jobs/{args.job_id}/log/',
                           token=CORUN_API_TOKEN)
        except Exception as e:
            log.warning('job log fetch failed: %s', e)
        _log('assessment_enforce', outcome='error', subject_key=campaign,
             sublevel='high', slot=slot, job_id=args.job_id,
             reason=f"run {args.status}: {str(run_log.get('error') or '')[:200]}")
        return 0

    page = _get(f'{CORUN_API_URL}/pages/{args.page_group_id}/',
                token=CORUN_API_TOKEN)
    content = page.get('content') or ''
    artifact, prose, problems = spec.extract_artifact(content)
    if artifact is not None:
        problems += spec.validate_artifact(artifact)
        problems += spec.validate_prose(prose, kind)

    if problems:
        is_repair = bool(submitted.get('repair'))
        if not is_repair and not _already_retried(args.prompt_group_id):
            repair_submission = dict(submitted)
            repair_submission['repair'] = {
                'validation_problems': problems,
                'previous_output': content,
                'instruction': (
                    'Produce a complete replacement report. Correct every '
                    'listed validation problem while preserving supported '
                    'production findings. Do not discuss the repair request '
                    'outside the Generation report.'),
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
                 reason='; '.join(problems)[:300])
            return 0
        # Second failure: quarantine — raw output retained, excluded from
        # later context (priors skip quarantined), verdict pinned to floor.
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
                      'elapsed_s': elapsed_s,
                      'job_id': args.job_id, 'enforcement': 'quarantined'}})
        _log('assessment_enforce', outcome='error', subject_key=campaign,
             sublevel='high', slot=slot, quarantined=True,
             corun_page_group_id=result.get('corun_page_group_id') or '',
             reason='quarantined after retry: ' + '; '.join(problems)[:250])
        return 0

    verdict = artifact.get('verdict')
    model_verdict = verdict
    floor_enforced = False
    if not spec.verdict_at_least(verdict, floor_verdict):
        floor_enforced = True
        artifact['verdict'] = verdict = floor_verdict

    narration = str(artifact.get('narration') or '').strip()
    result = _register_ai_assessment_sync(
        subject_type='campaign', subject_key=campaign,
        # Narration is structured metadata for compact UI consumers. The
        # human report must end with its provenance account.
        assessment=prose,
        username='assessment-harness', ai='corun-job',
        subject_label='', subject_url='',
        title=_report_title(prose, slot),
        data={'assessment_kind': kind, 'origin': 'scheduled',
              'schema_version': spec.SCHEMA_VERSION, 'slot': slot,
              'verdict': verdict, 'narration': narration,
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
                  'floor_enforced': floor_enforced}})
    if not result.get('success'):
        _log('assessment_enforce', outcome='error', subject_key=campaign,
             sublevel='high', slot=slot,
             reason=f"registration failed: {result.get('error')}")
        return 1
    _log('assessment_enforce', outcome='ok', subject_key=campaign, slot=slot,
         verdict=verdict, floor_enforced=floor_enforced,
         degraded=bool(bundle.get('degraded')),
         corun_page_group_id=result.get('corun_page_group_id') or '')
    print(f'{campaign} {slot}: registered verdict={verdict}'
          f'{" (floor-enforced)" if floor_enforced else ""}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
