"""Host certificate proposer (swf-monitor docs/PINGS.md § Entering a
ping; docs/AI_PROPOSALS.md, category ``ping``).

The same shape as the credential proposer, over the certificates the
PanDA and OSG service hosts serve. Two obligations come out of the
served chain: a ping on each certificate's expiry, due on the expiry
date with a seven-day lead, and a ping on a certificate served without
its intermediate, which a browser outside the grid trust configuration
cannot verify. Both are keyed on the host and, for the expiry, on the
date, so each is proposed once. A renewed certificate makes the open
ping's obligation met, and the rule proposes it fulfilled along with a
ping on the new date. Comments are code-filled. Rule-based: no model.

An unreachable host yields no ping, since there is no date to carry; the
certificate check's own action record reports it.

Run by the ``propose-certificate-pings.py`` doer, nightly as a
``catalog_sync`` chain step on the production ops agent, or by hand.
"""
import re
from datetime import timedelta

from .certificate_check import check_certificates

PROPOSER = 'certificate-expiry'
LEAD_DAYS = 7
OWNER = '@prodops'
# The chain obligation has no natural date of its own: it is due now, and
# the lead covers the whole span, so the ping is live from the moment it
# is entered.
CHAIN_DUE_DAYS = 7
CHAIN_LEAD_DAYS = 7


def ping_title(label):
    return f'Renew the {label} host certificate'


def chain_ping_title(label):
    return f'Serve the issuing intermediate certificate on {label}'


def _title_re(title):
    return re.compile(re.escape(title) + r'\b')


def findings(results=None):
    """One finding per host whose certificate was read: its expiry date,
    the days left, and whether an intermediate came with it."""
    out = []
    for entry in (results if results is not None else check_certificates()):
        if 'not_after' not in entry:
            continue
        out.append({
            'host': entry['host'],
            'label': entry['label'],
            'title': ping_title(entry['label']),
            'chain_title': chain_ping_title(entry['label']),
            'due': entry['not_after'],
            'days_left': entry['days_left'],
            'intermediate_served': entry.get('intermediate_served', True),
            'status': entry.get('status', ''),
        })
    return out


def _comment(finding):
    return (f'The certificate {finding["host"]} serves expires on '
            f'{finding["due"]}, {finding["days_left"]:.1f} days from the '
            f'check. Every client of that host fails to connect once it '
            f'expires.')


def _chain_comment(finding):
    return (f'{finding["host"]} serves its certificate without the issuing '
            f'intermediate, so a client that does not already hold that '
            f'intermediate cannot verify the host. Serving the full chain '
            f'is a web server configuration change on the host.')


def _fulfil_comment(finding, ping_due):
    return (f'The certificate {finding["host"]} serves now runs to '
            f'{finding["due"]}, past the {ping_due} this ping carries, so '
            f'the renewal it asks for has been done.')


def propose_certificate_pings(*, created_by='', batch_id='', apply=True):
    """Derive the findings and, with ``apply``, propose a ping for each
    expiry and each incomplete chain not already carried by an open ping,
    propose fulfilment of open pings whose certificate has since been
    renewed, and withdraw pending proposals whose finding no longer holds.
    Returns the findings and the propose results."""
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
    chain_due = (alarms_data._today_eastern()
                 + timedelta(days=CHAIN_DUE_DAYS)).isoformat()
    ping_items, live = [], set()
    for finding in found:
        pattern = _title_re(finding['title'])
        covering = [p for p in open_pings if pattern.match(p['title'] or '')]
        live.add((finding['title'], finding['due']))
        if not any(p['due'] == finding['due'] for p in covering):
            for ping in covering:
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
                'note': (f'The certificate is renewed by site administration '
                         f'on {finding["host"]}; the check reads what the '
                         f'host serves, so a renewed certificate closes this '
                         f'on its own.'),
            })
        if finding['intermediate_served']:
            continue
        chain_pattern = _title_re(finding['chain_title'])
        if any(chain_pattern.match(p['title'] or '') for p in open_pings):
            live.add((finding['chain_title'], chain_due))
            continue
        live.add((finding['chain_title'], chain_due))
        ping_items.append({
            'title': finding['chain_title'], 'due': chain_due,
            'lead_days': CHAIN_LEAD_DAYS, 'owner': OWNER,
            'comment': _chain_comment(finding),
            'note': (f'Configure the web server on {finding["host"]} to send '
                     f'the issuing intermediate with its certificate, then '
                     f'mark this fulfilled; the check reads the served chain '
                     f'and stops raising it.'),
        })
    if ping_items:
        result['pings'] = propose_pings(
            ping_items, proposer=PROPOSER, batch_id=batch_id,
            created_by=created_by)

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
