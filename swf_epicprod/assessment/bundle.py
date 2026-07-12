"""Evidence-bundle assembly — the harness front end's Task 1 basis.

Stdlib only. The must-look fetches run identically every time, each
recorded in the manifest with its outcome; a failure degrades the run
visibly, never silently. The bundle becomes the corun prompt content,
so corun's Prompt versioning archives every run's evidence — the
daily-cadence seed of the system-state-timeline direction.
"""

import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

TIMEOUT = 60
BUNDLE_SCHEMA = 'epicprod-evidence-bundle/1'
NARRATIVE_SECTION = 'epicprod.narrative'


def _get(url, token=''):
    headers = {'Accept': 'application/json'}
    if token:
        headers['Authorization'] = f'Token {token}'
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode() or '{}')


class _Manifest:
    def __init__(self):
        self.entries = []

    def fetch(self, source, url, token=''):
        t0 = time.monotonic()
        try:
            data = _get(url, token=token)
            self.entries.append({'source': source, 'url': url, 'ok': True,
                                 'ms': int((time.monotonic() - t0) * 1000)})
            return data
        except (urllib.error.URLError, urllib.error.HTTPError,
                json.JSONDecodeError, OSError) as e:
            self.entries.append({'source': source, 'url': url, 'ok': False,
                                 'error': str(e)[:300],
                                 'ms': int((time.monotonic() - t0) * 1000)})
            return None

    def note(self, source, ok, detail=''):
        entry = {'source': source, 'ok': ok}
        if detail:
            entry['detail' if ok else 'error'] = str(detail)[:300]
        self.entries.append(entry)

    @property
    def degraded(self):
        return any(not e['ok'] for e in self.entries)


def _page_items(listing):
    """The pages API returns {count, limit, offset, items: [...]}."""
    if isinstance(listing, list):
        return listing
    return listing.get('items') or listing.get('results') or []


def _previous_bundle(corun_url, token, section, campaign, kind, manifest):
    """The prior run's archived bundle (its prompt content) — the basis
    for deterministic night-over-night deltas. Numbers are compared in
    code; the model interprets the computed deltas."""
    listing = manifest.fetch(
        'previous_bundle', f'{corun_url}/sections/{section}/', token=token)
    if listing is None:
        return None
    prompts = listing.get('prompts') or []
    prefix = f'{campaign}/{kind}/'
    for prompt in prompts:  # newest first
        try:
            content = json.loads(prompt.get('content') or '{}')
        except json.JSONDecodeError:
            continue
        if str(content.get('slot') or '').startswith(prefix):
            return content.get('bundle') or None
    manifest.note('previous_bundle_match', True,
                  'no prior bundle for this campaign and kind (first run)')
    return None


def _deltas(previous, rollup):
    """Window-over-window movement, computed here — never by the model."""
    if not previous or not rollup:
        return {'available': False,
                'reason': 'no prior bundle to compare against'}
    prev_m = (previous.get('rollup') or {}).get('members') or {}
    cur_m = rollup.get('members') or {}

    def _n(members, member, *path):
        node = (members.get(member) or {}).get('data') or {}
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
            if node is None:
                return None
        return node

    out = {'available': True,
           'previous_generated_at': previous.get('generated_at') or ''}
    for label, member, path in (
            ('total_files', 'campaign_progress', ('total_files',)),
            ('outputs_complete', 'campaign_progress', ('outputs_complete',)),
            ('lifetime_jobs_finished', 'panda_health', ('jobs', 'nfinished')),
            ('lifetime_jobs_failed', 'panda_health', ('jobs', 'nfailed')),
    ):
        cur = _n(cur_m, member, *path)
        prev = _n(prev_m, member, *path)
        if cur is not None and prev is not None:
            out[label] = {'previous': prev, 'current': cur,
                          'delta': cur - prev}
    prev_disp = _n(prev_m, 'disposition_mix', 'dispositions') or {}
    cur_disp = _n(cur_m, 'disposition_mix', 'dispositions') or {}
    changed = {k: {'previous': prev_disp.get(k, 0), 'current': v}
               for k, v in cur_disp.items() if prev_disp.get(k, 0) != v}
    if changed:
        out['dispositions_changed'] = changed
    return out


def _find_narratives(pages, campaign):
    """Pick the campaign narrative and the latest general narrative from a
    narrative-section page listing (client-side: the pages API filters by
    section, names live in data)."""
    campaign_page = None
    general = None
    for page in pages or []:
        name = str((page.get('data') or {}).get('name') or '')
        if name == f'campaign_{campaign}':
            campaign_page = page
        elif name.startswith('campaign_general_'):
            if general is None or name > str((general.get('data') or {}).get('name') or ''):
                general = page
    return campaign_page, general


def assemble(campaign, kind, window_days, *, monitor_url, corun_url,
             corun_token='', section='epicprod.assessment'):
    """Build the evidence bundle for one assessment run."""
    manifest = _Manifest()

    # System status rides inside the rollup (an analytics member reading
    # the cached rows in-process) — no separately authenticated fetch.
    rollup = manifest.fetch(
        'campaign_status_rollup',
        f'{monitor_url}/pcs/api/campaigns/status/'
        f'?campaign={campaign}&window_days={window_days}')

    narratives = {'campaign': None, 'general': None}
    narrative_pages = manifest.fetch(
        'narratives',
        f'{corun_url}/pages/?section={NARRATIVE_SECTION}', token=corun_token)
    if narrative_pages is not None:
        pages = _page_items(narrative_pages)
        campaign_page, general_page = _find_narratives(pages, campaign)
        for label, page in (('campaign', campaign_page), ('general', general_page)):
            if page is not None:
                narratives[label] = {
                    'name': (page.get('data') or {}).get('name') or '',
                    'group_id': page.get('group_id') or '',
                    'version': page.get('version'),
                    'content': page.get('content') or '',
                }
            else:
                manifest.note(f'narrative_{label}', False,
                              f'no {label} narrative page found for {campaign}')

    prior_count = int((rollup or {}).get('assessment_prior_count') or 7)
    priors = []
    prior_pages = manifest.fetch(
        'prior_assessments',
        f'{corun_url}/pages/?section={section}'
        f'&subject_type=campaign&subject_key={campaign}', token=corun_token)
    if prior_pages is not None:
        for page in _page_items(prior_pages):
            data = page.get('data') or {}
            if data.get('assessment_kind') and data.get('assessment_kind') != kind:
                continue
            if data.get('quarantined'):
                continue
            structured = data.get('structured') or {}
            priors.append({
                'group_id': page.get('group_id') or '',
                'created_at': page.get('created_at') or '',
                'verdict': data.get('verdict') or '',
                'narration': (data.get('narration') or '')[:600],
                'slot': data.get('slot') or '',
                'standing_issues': structured.get('standing_issues') or [],
                'top_issues': [str(i.get('title') or '')[:120]
                               for i in structured.get('top_issues') or []],
            })
        priors.sort(key=lambda p: p['created_at'], reverse=True)
        priors = priors[:prior_count]

    previous = _previous_bundle(corun_url, corun_token, section,
                                campaign, kind, manifest)
    return {
        'schema': BUNDLE_SCHEMA,
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'params': {'campaign': campaign, 'kind': kind,
                   'window_days': window_days},
        'degraded': manifest.degraded,
        'manifest': manifest.entries,
        'rollup': rollup,
        'deltas': _deltas(previous, {'members': (rollup or {}).get('members')
                                     or {}, 'generated_at':
                                     (rollup or {}).get('generated_at')}),
        'narratives': narratives,
        'priors_supplied': len(priors),
        'priors': priors,
    }
