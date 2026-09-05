"""Campaign configuration proposer (swf-monitor docs/PINGS.md § Pings
with a remedy; docs/AI_PROPOSALS.md, category ``standard_config``).

The rule: every campaign edition that has tasks and no
``<edition> Standard Production`` configuration is a finding. Each
finding yields two proposals through the AI proposal subsystem: a ping,
the dated obligation to create the configuration, and its remedy, a
``standard_config`` proposal whose executor creates the configuration
from the template and marks the ping fulfilled in the same act. A
finding that no longer holds withdraws its pending proposals; an open
ping whose edition has since gained its configuration by hand gets a
fulfilment proposal. Comments are code-filled. Rule-based: no model.

Run by the ``propose-campaign-configs.py`` doer, nightly as a
``catalog_sync`` chain step on the production ops agent, or by hand.
"""
import re

from .models import ProdConfig, ProdTask
from .services import standard_prodconfig_image_present, standard_prodconfig_name

PROPOSER = 'campaign-config'
EDITION_RE = re.compile(r'^\d\d\.\d\d\.\d+$')
DUE_DAYS = 2
LEAD_DAYS = 1
OWNER = '@prodops'
TITLE_RE = re.compile(r'^Create the (\d\d\.\d\d\.\d+) Standard Production configuration$')


def ping_title(edition):
    return f'Create the {edition} Standard Production configuration'


def editions_in_use():
    """{edition: {'tasks': n, 'campaign': name, 'lifecycle': value}} over
    the tasks of campaigns that are not past."""
    rows = (ProdTask.objects.exclude(campaign__lifecycle='past')
            .values_list('dataset__detector_version', 'campaign__name',
                         'campaign__lifecycle'))
    out = {}
    for edition, campaign, lifecycle in rows:
        edition = str(edition or '').strip()
        if not EDITION_RE.match(edition):
            continue
        slot = out.setdefault(edition, {'tasks': 0, 'campaign': campaign,
                                        'lifecycle': lifecycle})
        slot['tasks'] += 1
    return out


def editions_without_standard_config():
    names = set(ProdConfig.objects.values_list('name', flat=True))
    return {edition: info for edition, info in editions_in_use().items()
            if standard_prodconfig_name(edition) not in names}


def _comment(edition, info):
    return (f'Campaign edition {edition} (campaign {info["campaign"]}, '
            f'{info["lifecycle"]}) has {info["tasks"]} tasks and no Standard '
            f'Production configuration; its tasks carry the import '
            f'placeholder and cannot be submitted or rerun from PCS.')


def propose_campaign_configs(*, created_by='', batch_id='', apply=True):
    """Derive the findings and, with ``apply``, submit the ping and remedy
    proposals, withdraw pending proposals whose finding no longer holds,
    and propose fulfilment of open pings whose edition now has its
    configuration. Returns the findings and the propose results."""
    from ai.models import Proposal
    from ai.services import (propose_ping_fulfil, propose_pings,
                             propose_standard_configs)
    from django.utils import timezone
    from monitor_app import alarms_data

    missing = editions_without_standard_config()
    today = alarms_data._today_eastern()
    from datetime import timedelta
    due = (today + timedelta(days=DUE_DAYS)).isoformat()
    findings, ping_items, remedy_items = [], [], []
    for edition, info in sorted(missing.items()):
        title = ping_title(edition)
        image_present = standard_prodconfig_image_present(edition)
        comment = _comment(edition, info)
        if not image_present:
            comment += (f' The campaign image eic_xl:{edition}-stable is not '
                        f'on CVMFS, so no remedy is proposed until it is.')
        findings.append({'edition': edition, 'title': title,
                         'image_present': image_present, **info})
        ping_items.append({
            'title': title, 'due': due, 'lead_days': LEAD_DAYS,
            'owner': OWNER, 'url': '/pcs/', 'comment': comment,
            'note': (f'Approve to create '
                     f'{standard_prodconfig_name(edition)} from the '
                     f'template and record this obligation met, or run '
                     f'scripts/create_standard_prodconfig.py --campaign '
                     f'{edition} --apply by hand.')})
        if image_present:
            remedy_items.append({'edition': edition, 'ping_title': title,
                                 'comment': comment})
    result = {'findings': findings, 'pings': None, 'remedies': None,
              'withdrawn': 0, 'fulfil_proposed': []}
    if not apply:
        return result
    if ping_items:
        result['pings'] = propose_pings(
            ping_items, proposer=PROPOSER, batch_id=batch_id,
            created_by=created_by)
    if remedy_items:
        result['remedies'] = propose_standard_configs(
            remedy_items, proposer=PROPOSER, batch_id=batch_id,
            created_by=created_by)
    # Findings that no longer hold: withdraw this proposer's pending rows.
    live_titles = {f['title'] for f in findings}
    live_names = {standard_prodconfig_name(f['edition']) for f in findings}
    now = timezone.now()
    for row in Proposal.objects.filter(proposer=PROPOSER, status='proposed',
                                       action__in=('ping', 'standard_config')):
        keep = ((row.action == 'ping'
                 and (row.payload or {}).get('title') in live_titles)
                or (row.action == 'standard_config'
                    and row.subject_key in live_names))
        if not keep:
            row.status = 'withdrawn'
            row.decided_at = now
            row.save(update_fields=['status', 'decided_at'])
            result['withdrawn'] += 1
    # Open pings of this rule whose configuration now exists by other means.
    names = set(ProdConfig.objects.values_list('name', flat=True))
    open_pings, _done = alarms_data.list_pings()
    for ping in open_pings:
        match = TITLE_RE.match(ping['title'] or '')
        if not match or standard_prodconfig_name(match.group(1)) not in names:
            continue
        outcome = propose_ping_fulfil(
            ping['id'],
            f'{standard_prodconfig_name(match.group(1))} exists; the '
            f'obligation is met.',
            proposer=PROPOSER, batch_id=batch_id, created_by=created_by)
        if outcome.get('proposed'):
            result['fulfil_proposed'].append(ping['title'])
    return result
