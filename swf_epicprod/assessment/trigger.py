"""Assessment harness front end — scheduled submission
(docs/EPICPROD_ASSESSMENTS_V1.md).

Stdlib only; runs from cron under the deployed venv python:

    45 3 * * *  python -m swf_epicprod.assessment.trigger --kind nightly
     0 6 * * 1  python -m swf_epicprod.assessment.trigger --kind weekly

Per target campaign: assemble the evidence bundle deterministically,
submit it as the run's prompt content (POST /prompts/), then the job
(POST /jobs/), and record an assessment_triggered action either way —
a slot that never fills must be visible.

Environment:
    SWF_MONITOR_URL              swf-monitor base URL incl. /swf-monitor.
    CORUN_API_URL                corun API base, e.g.
                                 https://epic-devcloud.org/doc/api/v1
    CORUN_API_TOKEN              corun API token.
    CORUN_ASSESSMENT_SECTION     assessment section (default
                                 epicprod.assessment).
    CORUN_ASSESSMENT_DEFINITION  campaign_assessment JobDefinition id
                                 (printed by assessment.bootstrap).
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

from swf_epicprod.assessment import bundle as bundle_mod
from swf_epicprod.assessment import spec

MONITOR_URL = os.environ.get('SWF_MONITOR_URL', '').rstrip('/')
CORUN_API_URL = (os.environ.get('CORUN_API_URL', '').rstrip('/')
                 or (os.environ.get('CORUN_BASE_URL', '').rstrip('/') + '/api/v1'
                     if os.environ.get('CORUN_BASE_URL') else ''))
CORUN_API_TOKEN = os.environ.get('CORUN_API_TOKEN', '')
CORUN_ASSESSMENT_SECTION = os.environ.get('CORUN_ASSESSMENT_SECTION',
                                          spec.DEFAULT_SECTION)
CORUN_ASSESSMENT_DEFINITION = os.environ.get('CORUN_ASSESSMENT_DEFINITION', '')
TIMEOUT = 30


def _request(url, payload=None, token=''):
    headers = {'Accept': 'application/json'}
    data = None
    if payload is not None:
        headers['Content-Type'] = 'application/json'
        data = json.dumps(payload).encode()
    if token:
        headers['Authorization'] = f'Token {token}'
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode() or '{}')


def log_action(action, *, outcome, subject_key='', reason='', **counts):
    """Post one epicprod action record; never raises (the trigger's own
    logging failure must not kill remaining submissions)."""
    extra = {'action': action, 'outcome': outcome, 'sublevel': 'normal',
             'live_default': outcome != 'ok'}
    if subject_key:
        extra['subject_key'] = str(subject_key)
        extra['subject_type'] = 'campaign'
    if reason:
        extra['reason'] = str(reason)[:300]
    extra.update(counts)
    message = ' '.join(x for x in (action, subject_key, outcome) if x)
    if reason:
        message += f' — {str(reason)[:300]}'
    try:
        _request(f'{MONITOR_URL}/api/logs/', payload={
            'app_name': 'epicprod',
            'instance_name': 'assessment-trigger',
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'level': 20 if outcome == 'ok' else 40,
            'levelname': 'INFO' if outcome == 'ok' else 'ERROR',
            'message': message,
            'module': 'assessment_trigger',
            'funcname': action,
            'lineno': 0,
            'process': os.getpid(),
            'thread': 0,
            'extra_data': extra,
        })
    except Exception as e:
        print(f'WARNING: action log post failed: {e}', file=sys.stderr)


def submit_run(campaign, kind, window_days, *, dry_run=False):
    """Assemble the bundle and create the corun run. Returns job id ''
    on dry runs; raises on submission failure."""
    evidence = bundle_mod.assemble(
        campaign, kind, window_days,
        monitor_url=MONITOR_URL, corun_url=CORUN_API_URL,
        corun_token=CORUN_API_TOKEN, section=CORUN_ASSESSMENT_SECTION)
    # The instructions live in the definition's SystemPrompt; the prompt
    # content is the run's data — slot and evidence bundle.
    date = evidence['generated_at'][:10]
    content = json.dumps({
        'slot': spec.slot(campaign, kind, date),
        'bundle': evidence,
    })
    if dry_run:
        print(f'{campaign}: dry run — bundle degraded={evidence["degraded"]}, '
              f'manifest={[(e["source"], e["ok"]) for e in evidence["manifest"]]}, '
              f'content {len(content)} bytes')
        return '', evidence
    prompt = _request(
        f'{CORUN_API_URL}/prompts/',
        payload={'section': CORUN_ASSESSMENT_SECTION, 'content': content,
                 'definition_id': CORUN_ASSESSMENT_DEFINITION},
        token=CORUN_API_TOKEN)
    job = _request(
        f'{CORUN_API_URL}/jobs/',
        payload={'prompt_group_id': str(prompt.get('group_id') or ''),
                 'definition_id': CORUN_ASSESSMENT_DEFINITION},
        token=CORUN_API_TOKEN)
    return str(job.get('id') or job.get('job_id') or ''), evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--kind', choices=('nightly', 'weekly'),
                        default='nightly')
    parser.add_argument('--window-days', type=float, default=None,
                        help='evidence window (default: 1 nightly, 7 weekly)')
    parser.add_argument('--campaign', default='',
                        help='override the target campaign(s)')
    parser.add_argument('--dry-run', action='store_true',
                        help='assemble and report the bundle; no submission')
    args = parser.parse_args()
    window_days = args.window_days or (7 if args.kind == 'weekly' else 1)

    if not MONITOR_URL:
        print('ERROR: SWF_MONITOR_URL is not set', file=sys.stderr)
        return 2
    if not args.dry_run and (not CORUN_API_URL or not CORUN_ASSESSMENT_DEFINITION):
        print('ERROR: CORUN_API_URL / CORUN_ASSESSMENT_DEFINITION not set',
              file=sys.stderr)
        log_action('assessment_triggered', outcome='error',
                   reason='CORUN_API_URL or CORUN_ASSESSMENT_DEFINITION unset')
        return 2

    try:
        status = _request(
            f'{MONITOR_URL}/pcs/api/campaigns/status/?targets_only=1')
    except Exception as e:
        print(f'ERROR: target resolution failed: {e}', file=sys.stderr)
        log_action('assessment_triggered', outcome='error',
                   reason=f'target resolution failed: {e}')
        return 1

    if not status.get('assessment_enabled', True):
        print('assessments disabled (SysConfig assessment_enabled)')
        return 0
    targets = ([args.campaign] if args.campaign
               else status.get('targets') or [])
    if not targets:
        log_action('assessment_triggered', outcome='error',
                   reason='no producing or current campaign to assess')
        return 1

    failures = 0
    for campaign in targets:
        try:
            job_id, evidence = submit_run(campaign, args.kind, window_days,
                                          dry_run=args.dry_run)
            if args.dry_run:
                continue
            print(f'{campaign}: {args.kind} assessment job {job_id} created'
                  f'{" (degraded evidence)" if evidence["degraded"] else ""}')
            log_action('assessment_triggered', outcome='ok',
                       subject_key=campaign, kind=args.kind,
                       window_days=window_days, job_id=job_id,
                       degraded=evidence['degraded'])
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors='replace')[:300]
            print(f'ERROR: {campaign}: run creation failed: {e.code} {body}',
                  file=sys.stderr)
            log_action('assessment_triggered', outcome='error',
                       subject_key=campaign, kind=args.kind,
                       reason=f'HTTP {e.code}: {body}')
            failures += 1
        except Exception as e:
            print(f'ERROR: {campaign}: run creation failed: {e}',
                  file=sys.stderr)
            log_action('assessment_triggered', outcome='error',
                       subject_key=campaign, kind=args.kind, reason=str(e))
            failures += 1
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
