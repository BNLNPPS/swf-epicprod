"""Evidence-bundle assembly — the harness front end's Task 1 basis.

Stdlib only. The must-look fetches run identically every time, each
recorded in the manifest with its outcome; a failure degrades the run
visibly, never silently. Production analytics owns the state history used
for comparisons. Generated assessments are consumers, never evidence stores.
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from swf_epicprod.assessment import reporting

TIMEOUT = 60
BUNDLE_SCHEMA = 'epicprod-evidence-bundle/4'
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
                                 'error': str(e),
                                 'ms': int((time.monotonic() - t0) * 1000)})
            return None

    def note(self, source, ok, detail=''):
        entry = {'source': source, 'ok': ok}
        if detail:
            entry['detail' if ok else 'error'] = str(detail)
        self.entries.append(entry)

    @property
    def degraded(self):
        return any(not e['ok'] for e in self.entries)


def _page_items(listing):
    """The pages API returns {count, limit, offset, items: [...]}."""
    if isinstance(listing, list):
        return listing
    return listing.get('items') or listing.get('results') or []


def _parse_timestamp(value):
    try:
        parsed = datetime.fromisoformat(str(value or '').replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _baseline_status(monitor_url, campaign, generated_at, comparison_days,
                     manifest):
    """Production analytics snapshot closest to one reporting window earlier."""
    target = generated_at - timedelta(days=comparison_days)
    query = urllib.parse.urlencode({
        'campaign': campaign,
        'history_at': target.isoformat(),
    })
    return manifest.fetch(
        'campaign_status_baseline',
        f'{monitor_url}/pcs/api/campaigns/status/?{query}')


def _deltas(baseline, rollup, generated_at, comparison_days):
    """Movement from recorded state closest to one reporting window earlier."""
    target = generated_at - timedelta(days=comparison_days)
    target_hours = comparison_days * 24
    if not rollup:
        return {'available': False, 'reason': 'current rollup unavailable'}
    if not baseline or not baseline.get('available'):
        return {
            'available': False,
            'target_generated_at': target.isoformat(),
            'target_span_hours': target_hours,
            'reason': str((baseline or {}).get('reason')
                          or 'production analytics history unavailable'),
        }
    previous = baseline.get('status') or {}
    prev_m = previous.get('members') or {}
    cur_m = rollup.get('members') or {}
    baseline_at = _parse_timestamp(
        baseline.get('selected_at') or previous.get('generated_at'))

    def _n(members, member, *path):
        node = (members.get(member) or {}).get('data') or {}
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
            if node is None:
                return None
        return node

    out = {
        'available': True,
        'basis': (
            'recorded production analytics closest to one reporting window '
            'before the current state'),
        'target_generated_at': target.isoformat(),
        'target_span_hours': target_hours,
        'baseline_generated_at': (baseline.get('selected_at')
                                  or previous.get('generated_at') or ''),
        'baseline_distance_hours': baseline.get('distance_hours'),
    }
    if baseline_at is not None:
        out['elapsed_hours'] = round(
            (generated_at - baseline_at).total_seconds() / 3600, 3)
    for label, member, path in (
            ('task_count', 'campaign_progress', ('task_count',)),
            ('tasks_with_processing', 'campaign_progress',
             ('tasks_with_processing',)),
            ('outputs_total', 'campaign_progress', ('outputs_total',)),
            ('total_files', 'campaign_progress', ('total_files',)),
            ('total_bytes', 'campaign_progress', ('total_bytes',)),
            ('outputs_placement_complete', 'campaign_progress',
             ('outputs_placement_complete',)),
            ('panda_task_count', 'panda_health', ('panda_task_count',)),
            ('lifetime_jobs_finished', 'panda_health', ('jobs', 'nfinished')),
            ('lifetime_jobs_final_failed', 'panda_health',
             ('jobs', 'nfinalfailed')),
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


def _campaign_family(campaign):
    """The narrative-bearing campaign family: the first two name fields.
    The third field discriminates editions within the family (26.07.0,
    26.07.1) that share one narrative."""
    parts = str(campaign).split('.')
    return '.'.join(parts[:2]) if len(parts) >= 2 else str(campaign)


def _edition_order(name):
    """Sort key for edition-suffixed narrative names, numeric-aware so
    campaign_26.07.10 outranks campaign_26.07.9."""
    tail = name.rsplit('.', 1)[-1]
    if tail.isdigit():
        return (0, int(tail), '')
    return (1, 0, name)


def _find_narratives(pages, campaign):
    """Pick the campaign narrative and the latest general narrative from a
    narrative-section page listing (client-side: the pages API filters by
    section, names live in data). The campaign narrative belongs to the
    family, so the campaign's edition field is ignored: a bare
    campaign_<family> page wins outright, else the highest-edition
    campaign_<family>.<N> page is the family narrative."""
    family = _campaign_family(campaign)
    bare = f'campaign_{family}'
    bare_page = None
    editions = []
    general = None
    for page in pages or []:
        name = str((page.get('data') or {}).get('name') or '')
        if name == bare:
            bare_page = page
        elif name.startswith(f'{bare}.'):
            editions.append((name, page))
        elif name.startswith('campaign_general_'):
            if general is None or name > str((general.get('data') or {}).get('name') or ''):
                general = page
    campaign_page = bare_page
    if campaign_page is None and editions:
        campaign_page = max(editions, key=lambda item: _edition_order(item[0]))[1]
    return campaign_page, general


def assemble(campaign, kind, window_days, *, monitor_url, corun_url,
             corun_token='', section='epicprod.assessment'):
    """Build the evidence bundle for one assessment run."""
    manifest = _Manifest()
    generated_at = datetime.now(timezone.utc)

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
                if label == 'campaign':
                    narratives[label]['scope'] = (
                        f'This is the campaign narrative for {campaign}: '
                        f'narratives belong to the campaign family '
                        f'({_campaign_family(campaign)}), whose editions '
                        f'share one narrative.')
            else:
                manifest.note(f'narrative_{label}', False,
                              f'no {label} narrative page found for {campaign}')

    baseline = _baseline_status(
        monitor_url, campaign, generated_at, window_days, manifest)
    deltas = _deltas(baseline, rollup, generated_at, window_days)
    evidence = {
        'schema': BUNDLE_SCHEMA,
        'generated_at': generated_at.isoformat(),
        'params': {'campaign': campaign, 'kind': kind,
                   'window_days': window_days},
        'degraded': manifest.degraded,
        'degraded_meaning': (
            'one or more required bundle fetches failed; this field does not '
            'assert semantic consistency or source freshness'),
        'manifest': manifest.entries,
        'rollup': rollup,
        'deltas': deltas,
        'narratives': narratives,
        'prior_ai_reports_supplied': 0,
    }
    evidence['facts'] = reporting.build_fact_set(rollup, deltas)
    return evidence
