#!/usr/bin/env python3
"""Scheduled trigger for campaign assessments (docs/EPICPROD_ASSESSMENTS_V1.md).

Stdlib-only. Reads the assessment targets and enable gate from the
campaign-status REST endpoint, creates one corun-ai `epicprod-assessment`
job per target campaign, and records an `assessment_triggered` action in
the epicprod action stream for every attempt — a slot that never fills
must be visible.

Cron (after the 02:15 catalog_sync chain refreshes the assessed state):

    45 3 * * *  assessment-trigger.py --kind nightly
     0 6 * * 1  assessment-trigger.py --kind weekly --window-days 7

Environment:
    SWF_MONITOR_URL              swf-monitor base URL including the
                                 /swf-monitor path (as in production.env).
    CORUN_API_URL                corun-ai API base, e.g.
                                 https://epic-devcloud.org/doc/api/v1
    CORUN_API_TOKEN              corun-ai API token.
    CORUN_ASSESSMENT_SECTION     corun Section name for assessment intake.
    CORUN_ASSESSMENT_DEFINITION  campaign_assessment JobDefinition id.

Run creation follows the deployed codoc API contract: POST /prompts/
{section, content, definition_id} with the run parameters as JSON
content, then POST /jobs/ {prompt_group_id, definition_id}.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

MONITOR_URL = os.environ.get('SWF_MONITOR_URL', '').rstrip('/')
CORUN_API_URL = os.environ.get('CORUN_API_URL', '').rstrip('/')
CORUN_API_TOKEN = os.environ.get('CORUN_API_TOKEN', '')
CORUN_ASSESSMENT_SECTION = os.environ.get('CORUN_ASSESSMENT_SECTION',
                                          'epicprod.assessment')
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
    failure to log must not kill the remaining job submissions)."""
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


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--kind', choices=('nightly', 'weekly'),
                        default='nightly')
    parser.add_argument('--window-days', type=float, default=None,
                        help='evidence window (default: 1 nightly, 7 weekly)')
    parser.add_argument('--campaign', default='',
                        help='override the target campaign(s)')
    args = parser.parse_args()
    window_days = args.window_days or (7 if args.kind == 'weekly' else 1)

    if not MONITOR_URL:
        print('ERROR: SWF_MONITOR_URL is not set', file=sys.stderr)
        return 2
    if not CORUN_API_URL or not CORUN_ASSESSMENT_DEFINITION:
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
        # The deployed codoc API contract (corun_app/api): run creation is
        # two POSTs — a Prompt version carrying the run parameters as JSON
        # content, then the Job referencing its prompt group.
        params = {
            'campaign': campaign,
            'kind': args.kind,
            'window_days': window_days,
            'requested_by': 'assessment-trigger',
        }
        try:
            prompt = _request(
                f'{CORUN_API_URL}/prompts/',
                payload={'section': CORUN_ASSESSMENT_SECTION,
                         'content': json.dumps(params),
                         'definition_id': CORUN_ASSESSMENT_DEFINITION},
                token=CORUN_API_TOKEN)
            job = _request(
                f'{CORUN_API_URL}/jobs/',
                payload={'prompt_group_id': str(prompt.get('group_id') or ''),
                         'definition_id': CORUN_ASSESSMENT_DEFINITION},
                token=CORUN_API_TOKEN)
            job_id = str(job.get('id') or job.get('job_id') or '')
            print(f'{campaign}: {args.kind} assessment job {job_id} created')
            log_action('assessment_triggered', outcome='ok',
                       subject_key=campaign, kind=args.kind,
                       window_days=window_days, job_id=job_id)
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors='replace')[:300]
            print(f'ERROR: {campaign}: corun-ai job creation failed: '
                  f'{e.code} {body}', file=sys.stderr)
            log_action('assessment_triggered', outcome='error',
                       subject_key=campaign, kind=args.kind,
                       reason=f'HTTP {e.code}: {body}')
            failures += 1
        except Exception as e:
            print(f'ERROR: {campaign}: corun-ai job creation failed: {e}',
                  file=sys.stderr)
            log_action('assessment_triggered', outcome='error',
                       subject_key=campaign, kind=args.kind, reason=str(e))
            failures += 1
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
