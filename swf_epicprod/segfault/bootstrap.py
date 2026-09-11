"""Bootstrap the segfault diagnosis configuration in corun-ai over REST.

Stdlib only; idempotent. Creates or updates the segfault section and its
bundle section, the system prompt (from ``spec``, versioned in place when
the text changes), and the ``segfault_diagnosis`` JobDefinition, then
prints the environment value the trigger and enforcement read
(``CORUN_SEGFAULT_DEFINITION``). The corun client and the definition's
tool set are the assessment bootstrap's.

    python -m swf_epicprod.segfault.bootstrap [--model gpt-5.6-sol]
        [--effort xhigh] [--timeout-s 1800]

Environment: CORUN_API_URL, CORUN_API_TOKEN.
"""
import argparse
import sys
import urllib.error
import urllib.request

from swf_epicprod.assessment.bootstrap import (CORUN_API_TOKEN, CORUN_API_URL,
                                               _request, ensure_definition,
                                               ensure_section)
from swf_epicprod.segfault import spec


def ensure_system_prompt():
    wanted = spec.system_prompt_text()
    name = spec.SYSTEM_PROMPT_TITLE
    listing = _request('GET', f'/system-prompts/?name={urllib.request.quote(name)}')
    rows = listing if isinstance(listing, list) else listing.get('results') or []
    current = next((r for r in rows if r.get('is_current', True)), None)
    if current and (current.get('content') or '').strip() == wanted.strip():
        print(f"system prompt: current (group {current.get('group_id')}, "
              f"v{current.get('version')})")
        return str(current['group_id'])
    payload = {'name': name, 'content': wanted,
               'data': {'source': 'swf_epicprod.segfault.spec'}}
    if current:
        payload['group_id'] = current['group_id']
    created = _request('POST', '/system-prompts/', payload)
    print(f"system prompt: {'new version' if current else 'created'} "
          f"(group {created.get('group_id')}, v{created.get('version')})")
    return str(created['group_id'])


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--model', default='gpt-5.6-sol')
    parser.add_argument('--effort', default='xhigh')
    parser.add_argument('--timeout-s', type=int, default=spec.WORKER_TIMEOUT_S)
    args = parser.parse_args()
    if not CORUN_API_URL or not CORUN_API_TOKEN:
        print('ERROR: CORUN_API_URL / CORUN_API_TOKEN not set', file=sys.stderr)
        return 2
    ensure_section(spec.DEFAULT_SECTION, title='epicprod segfault diagnoses',
                   description='Segfault diagnosis runs and artifacts '
                               '(swf-epicprod/docs/SEGFAULT_DIAGNOSIS.md).')
    ensure_section(spec.DEFAULT_BUNDLE_SECTION,
                   title='epicprod segfault diagnosis evidence bundles',
                   description='The evidence bundle of each segfault diagnosis '
                               'run; hidden from normal corun presentation.',
                   ui_visible=False)
    sp_group = ensure_system_prompt()
    definition_id = ensure_definition(
        spec.DEFINITION_NAME, sp_group, args.model, args.effort, args.timeout_s,
        description='epicprod segfault diagnosis — the model run only; the '
                    'harness is production-side (SEGFAULT_DIAGNOSIS.md).')
    print('\nEnvironment for the trigger and enforcement:')
    print(f'CORUN_SEGFAULT_SECTION={spec.DEFAULT_SECTION}')
    print(f'CORUN_SEGFAULT_BUNDLE_SECTION={spec.DEFAULT_BUNDLE_SECTION}')
    print(f'CORUN_SEGFAULT_DEFINITION={definition_id}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
