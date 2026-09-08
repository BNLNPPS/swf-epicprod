"""Credential expiry proposer (swf-monitor docs/PINGS.md § Entering a
ping; docs/AI_PROPOSALS.md, category ``ping``).

The rule: every production credential whose expiry can be read is a
dated obligation. For each one with no open ping on that expiry, propose
a ping due on the expiry date with a seven-day lead, keyed on the
credential and the date, so the proposal is made once. When a credential
is renewed, its open ping's obligation is met: the rule proposes that
ping fulfilled and a ping on the new date. Comments are code-filled.
Rule-based: no model.

A credential that is unset, missing, or unreadable yields no ping, since
there is no date to carry; the credential check's own action record
reports it.

Run by the ``propose-credential-pings.py`` doer, nightly as a
``catalog_sync`` chain step on the production ops agent, or by hand.
"""
import re
import time
from datetime import date

from .credential_check import check_credentials

PROPOSER = 'credential-expiry'
LEAD_DAYS = 7
OWNER = '@prodops'
LABELS = {
    'panda_oidc_token': 'PanDA OIDC token',
    'bnl_rucio_proxy': 'BNL Rucio proxy',
    'evgen_output_proxy': 'EVGEN output proxy',
}
NOTES = {
    'panda_oidc_token': (
        'The token is renewed by a fresh OIDC device flow in an '
        'interactive shell under the operating account: source '
        '~/pclient/run/setup.sh, remove $PANDA_CONFIG_ROOT/.token, then run '
        'any prun command and consent as EIC.production. Automated '
        'submission stops when it expires (swf-epicprod '
        'docs/EPICPROD_OPS.md, Identity and client).'),
    'bnl_rucio_proxy': (
        'The BNL Rucio proxy authenticates payload-log retrieval and every '
        'Rucio metadata read. Renewal is an operating-account action; the '
        'renewed proxy replaces the file the environment names, and the '
        'copy the web tier and the ops agent share.'),
    'evgen_output_proxy': (
        'The EVGEN output proxy travels in the submission sandbox and '
        'registers job output in JLab Rucio. Renewal is an '
        'operating-account action; jobs submitted after it expires fail '
        'their registration.'),
}
# Any open ping whose title begins with the obligation, by hand or by this
# rule, is this rule's obligation; a longer hand-written title counts.
TITLE_PREFIX = 'Renew the '


def ping_title(credential):
    return f'{TITLE_PREFIX}{LABELS.get(credential, credential)}'


def _title_re(credential):
    return re.compile(re.escape(ping_title(credential)) + r'\b')


def _expiry_date(days_left):
    """The expiry as a date, from the days the check measured."""
    return date.fromtimestamp(time.time() + days_left * 86400.0)


def findings(results=None):
    """One finding per credential whose expiry is known: its label, the
    expiry date, and the days left."""
    out = []
    for entry in (results if results is not None else check_credentials()):
        if 'days_left' not in entry:
            continue
        credential = entry['credential']
        out.append({
            'credential': credential,
            'label': LABELS.get(credential, credential),
            'title': ping_title(credential),
            'due': _expiry_date(entry['days_left']).isoformat(),
            'days_left': entry['days_left'],
            'status': entry.get('status', ''),
            'path': entry.get('path', ''),
        })
    return out


def _comment(finding):
    return (f'The {finding["label"]} expires on {finding["due"]}, '
            f'{finding["days_left"]:.1f} days from the check. Production '
            f'automation that depends on it stops when it expires.')


def _fulfil_comment(finding, ping_due):
    return (f'The {finding["label"]} now runs to {finding["due"]}, past the '
            f'{ping_due} this ping carries, so the renewal it asks for has '
            f'been done.')


def propose_credential_pings(*, created_by='', batch_id='', apply=True):
    """Derive the findings and, with ``apply``, propose a ping for each
    expiry not already carried by an open ping, propose fulfilment of open
    pings whose credential has since been renewed, and withdraw pending
    proposals whose finding no longer holds. Returns the findings and the
    propose results."""
    from ai.models import Proposal
    from ai.services import propose_ping_fulfil, propose_pings
    from django.utils import timezone
    from monitor_app import alarms_data

    found = findings()
    result = {'findings': found, 'pings': None, 'fulfil_proposed': [],
              'withdrawn': 0, 'errors': []}
    if not apply:
        return result

    open_pings, _done = alarms_data.list_pings()
    ping_items = []
    for finding in found:
        pattern = _title_re(finding['credential'])
        covering = [p for p in open_pings if pattern.match(p['title'] or '')]
        if any(p['due'] == finding['due'] for p in covering):
            continue
        for ping in covering:
            # An open ping on an earlier date: the credential was renewed
            # past it, which is the obligation met.
            if ping['due'] and ping['due'] < finding['due']:
                try:
                    propose_ping_fulfil(
                        ping['id'], _fulfil_comment(finding, ping['due']),
                        proposer=PROPOSER, batch_id=batch_id,
                        created_by=created_by)
                    result['fulfil_proposed'].append(ping['title'])
                except Exception as e:  # ServiceError and friends
                    result['errors'].append(f'{ping["title"]}: {e}')
        ping_items.append({
            'title': finding['title'], 'due': finding['due'],
            'lead_days': LEAD_DAYS, 'owner': OWNER,
            'comment': _comment(finding),
            'note': NOTES.get(finding['credential'], ''),
        })
    if ping_items:
        result['pings'] = propose_pings(
            ping_items, proposer=PROPOSER, batch_id=batch_id,
            created_by=created_by)

    live = {(f['title'], f['due']) for f in found}
    now = timezone.now()
    for row in Proposal.objects.filter(proposer=PROPOSER, status='proposed',
                                       action='ping'):
        payload = row.payload or {}
        if (payload.get('title'), payload.get('due')) in live:
            continue
        row.status = 'withdrawn'
        row.decided_at = now
        row.save(update_fields=['status', 'decided_at'])
        result['withdrawn'] += 1
    return result
