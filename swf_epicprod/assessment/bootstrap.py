"""Bootstrap epicprod's assessment configuration in corun-ai over REST.

Stdlib only; idempotent — safe to rerun. Creates or updates the
assessment section, the system prompt (from ``spec``, versioned in
place when the text changes), and the ``campaign_assessment``
JobDefinition, then prints the environment values the trigger and
enforcement need. No human hands on corun configuration.

    python -m swf_epicprod.assessment.bootstrap [--model gpt-5.6-sol]
        [--effort xhigh] [--timeout-s 900]

Environment: CORUN_API_URL, CORUN_API_TOKEN.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

from swf_epicprod.assessment import spec

CORUN_API_URL = (os.environ.get('CORUN_API_URL', '').rstrip('/')
                 or (os.environ.get('CORUN_BASE_URL', '').rstrip('/') + '/api/v1'
                     if os.environ.get('CORUN_BASE_URL') else ''))
CORUN_API_TOKEN = os.environ.get('CORUN_API_TOKEN', '')
TIMEOUT = 30


def _request(method, path, payload=None):
    headers = {'Accept': 'application/json',
               'Authorization': f'Token {CORUN_API_TOKEN}'}
    data = None
    if payload is not None:
        headers['Content-Type'] = 'application/json'
        data = json.dumps(payload).encode()
    req = urllib.request.Request(f'{CORUN_API_URL}{path}', data=data,
                                 headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode() or '{}')


def ensure_section(name):
    try:
        _request('GET', f'/sections/{name}/')
        print(f'section {name}: exists')
        return
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
    _request('POST', '/sections/', {
        'name': name,
        'title': 'epicprod campaign assessments',
        'description': 'Scheduled campaign assessment runs and artifacts '
                       '(swf-epicprod/docs/EPICPROD_ASSESSMENTS_V1.md).',
        'data': {'ui_visible': False},
    })
    print(f'section {name}: created')


def ensure_system_prompt():
    wanted = spec.system_prompt_text()
    listing = _request(
        'GET', f'/system-prompts/?name={urllib.request.quote(spec.DEFAULT_SYSTEM_PROMPT_TITLE)}')
    rows = listing if isinstance(listing, list) else listing.get('results') or []
    current = next((r for r in rows if r.get('is_current', True)), None)
    if current and (current.get('content') or '') == wanted:
        print(f"system prompt: current (group {current.get('group_id')}, "
              f"v{current.get('version')})")
        return str(current['group_id'])
    payload = {'name': spec.DEFAULT_SYSTEM_PROMPT_TITLE, 'content': wanted,
               'data': {'source': 'swf_epicprod.assessment.spec'}}
    if current:
        payload['group_id'] = current['group_id']
    created = _request('POST', '/system-prompts/', payload)
    print(f"system prompt: {'new version' if current else 'created'} "
          f"(group {created.get('group_id')}, v{created.get('version')})")
    return str(created['group_id'])


def ensure_definition(name, sp_group_id, model, effort, timeout_s):
    wanted_data = {'model': model, 'effort': effort,
                   'mcp_tools': ['swf-testbed'],
                   'system_prompt_group_id': sp_group_id,
                   'timeout_s': timeout_s}
    listing = _request('GET', '/definitions/')
    rows = listing if isinstance(listing, list) else listing.get('results') or []
    existing = next((r for r in rows if r.get('name') == name), None)
    if existing:
        if (existing.get('data') or {}) == wanted_data:
            print(f"definition {name}: current ({existing['id']})")
            return str(existing['id'])
        _request('PATCH', f"/definitions/{existing['id']}/",
                 {'data': wanted_data})
        print(f"definition {name}: updated ({existing['id']})")
        return str(existing['id'])
    created = _request('POST', '/definitions/', {
        'name': name,
        'description': 'epicprod campaign assessment — the model run only; '
                       'the harness is production-side '
                       '(EPICPROD_ASSESSMENTS_V1.md).',
        'status': 'active',
        'data': wanted_data,
    })
    print(f"definition {name}: created ({created.get('id')})")
    return str(created['id'])


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--model', default='gpt-5.6-sol')
    parser.add_argument('--effort', default='xhigh')
    parser.add_argument('--timeout-s', type=int, default=900)
    parser.add_argument('--section', default=spec.DEFAULT_SECTION)
    parser.add_argument('--definition-name',
                        default=spec.DEFAULT_DEFINITION_NAME)
    args = parser.parse_args()

    if not CORUN_API_URL or not CORUN_API_TOKEN:
        print('ERROR: CORUN_API_URL / CORUN_API_TOKEN not set',
              file=sys.stderr)
        return 2

    ensure_section(args.section)
    sp_group = ensure_system_prompt()
    definition_id = ensure_definition(
        args.definition_name, sp_group, args.model, args.effort,
        args.timeout_s)

    print('\nEnvironment for the trigger and enforcement:')
    print(f'CORUN_ASSESSMENT_SECTION={args.section}')
    print(f'CORUN_ASSESSMENT_DEFINITION={definition_id}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
