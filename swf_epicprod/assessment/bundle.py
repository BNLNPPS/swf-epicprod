"""Evidence-bundle assembly — the harness front end's Task 1 basis.

Stdlib only. The must-look fetches run identically every time, each
recorded in the manifest with its outcome; a failure degrades the run
visibly, never silently. Production analytics owns the state history used
for comparisons. Generated assessments are consumers, never evidence stores.
"""

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from swf_epicprod.assessment import reporting

TIMEOUT = 60
BUNDLE_SCHEMA = 'epicprod-evidence-bundle/4'
NARRATIVE_SECTION = 'epicprod.narrative'
LIVE_CHANNEL = 'epicprod-live'
LIVE_MAX_PAGES = 20


def _get(url, token='', *, auth_scheme='Token'):
    headers = {'Accept': 'application/json'}
    if token:
        headers['Authorization'] = f'{auth_scheme} {token}'
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode() or '{}')


class _Manifest:
    def __init__(self):
        self.entries = []

    def fetch(self, source, url, token='', *, auth_scheme='Token'):
        t0 = time.monotonic()
        try:
            data = _get(url, token=token, auth_scheme=auth_scheme)
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


def _live_findings(generated_at, window_days, manifest):
    """Read the reporting channel, not generated assessments, as evidence.

    Uses the trigger's existing Mattermost credential, only for GETs. A
    failed or bounded-incomplete read is a visible evidence limitation.
    Nothing is joined, posted, rewritten, or backfilled.
    """
    start = generated_at - timedelta(days=window_days)
    result = {
        'channel': LIVE_CHANNEL, 'available': False, 'complete': False,
        'window_start': start.isoformat(), 'window_end': generated_at.isoformat(),
        'posts': [], 'excluded_generated_assessments': 0,
        'assessment_requirement': (
            'Read these channel posts in full before assessing material '
            'failures. Reconcile findings and resolution notices with the '
            'same incident, endpoint, tasks and time interval; verify their '
            'linked evidence when material. Distinguish resolved historical '
            'losses from current unresolved problems. Cite the posts and '
            'supporting evidence. If this read is unavailable or incomplete, '
            'report that limitation, never infer that no resolution exists.'),
    }
    token = os.environ.get('EPICPROD_LIVE_TOKEN') or os.environ.get('MATTERMOST_TOKEN')
    if not token:
        manifest.note('epicprod_live_findings', False, 'Mattermost read credential unavailable')
        return result
    host = os.environ.get('MATTERMOST_URL', 'chat.epic-eic.org').removeprefix('https://').rstrip('/')
    team_name = os.environ.get('MATTERMOST_TEAM', 'main')
    base = f'https://{host}'

    def fetch(source, path):
        return manifest.fetch(source, base + '/api/v4' + path,
                              token=token, auth_scheme='Bearer')

    team = fetch('epicprod_live_team', '/teams/name/' + urllib.parse.quote(team_name, safe=''))
    if not team:
        return result
    channel = fetch('epicprod_live_channel', f'/teams/{team["id"]}/channels/name/{LIVE_CHANNEL}')
    if not channel:
        return result
    since_ms = int(start.timestamp() * 1000)
    until_ms = int(generated_at.timestamp() * 1000)
    posts = {}
    for page in range(LIVE_MAX_PAGES):
        data = fetch('epicprod_live_posts',
                     f'/channels/{channel["id"]}/posts?page={page}&per_page=100')
        if data is None:
            break
        batch = [data['posts'][key] for key in data.get('order', [])]
        for post in batch:
            if (since_ms <= post['create_at'] <= until_ms
                    and not post.get('delete_at') and not post.get('type')):
                posts[post['id']] = post
        if not batch or min(p['create_at'] for p in batch) < since_ms:
            result.update(available=True, complete=True)
            break
    if not result['complete']:
        manifest.note('epicprod_live_findings', False, 'channel window could not be read completely')
    for post in sorted(posts.values(), key=lambda p: (p['create_at'], p['id'])):
        text = post.get('message') or ''
        # These are generated reports, not independent findings. Exclude both
        # linked publication cards and legacy action-title notices.
        if ((text.startswith('### [') and re.search(
                r'\*\*(?:[A-Za-z_ ]+ )?AI assessment published\*\*', text))
                or re.match(r'^`[^`]+` · \*\*assessment register\*\*', text)):
            result['excluded_generated_assessments'] += 1
            continue
        result['posts'].append({
            'id': post['id'], 'author_id': post.get('user_id') or '',
            'timestamp': datetime.fromtimestamp(post['create_at'] / 1000, timezone.utc).isoformat(),
            'thread_id': post.get('root_id') or '', 'content': text,
            'url': f'{base}/{team_name}/pl/{post["id"]}',
        })
    return result


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
    live_findings = _live_findings(generated_at, window_days, manifest)
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
        'live_findings': live_findings,
        'prior_ai_reports_supplied': 0,
    }
    evidence['facts'] = reporting.build_fact_set(rollup, deltas)
    return evidence
