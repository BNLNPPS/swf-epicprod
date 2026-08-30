"""Derive expected-events targets for a campaign's physics
configurations that have none (CAMPAIGN_DELIVERY.md, Completion).

The rules and their evidence live in
``swf_epicprod/analytics/completion.py``; this wrapper lists the
proposals per rule and, with --apply, writes them through the
expected-events service with source ``derived`` — one service call
per rule, each carrying the rule's comment, so the action stream
records one event per rule with its count. Existing targets of any
source are never overwritten. Targets at or above the implausible
bound (1e9 events) are listed and, with --apply, cleared with a
comment. Dry-run default; re-running is idempotent (a proposal equal
to the recorded value is a no-op in the service).

Run under the venv with the swf-monitor project on the path:

    cd <swf-monitor>/src && source <venv>/bin/activate && source ~/.env
    python <swf-epicprod>/scripts/derive_expected_events.py \\
        [--campaign 26.07] [--apply] [--changed-by <user>]

Without --campaign, the current campaign is used.
"""

import argparse
import collections
import os
import sys

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402

django.setup()

from swf_epicprod.analytics.completion import (  # noqa: E402
    IMPLAUSIBLE_TARGET, derive_expected_events)

RULE_TITLES = {
    'R1': 'round closure of delivered events',
    'R2': 'prior campaign delivered events',
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--campaign', default='',
                        help='campaign family (default: the current campaign)')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--changed-by', default='derive_expected_events')
    parser.add_argument('--show', type=int, default=8,
                        help='proposals to print per rule (dry-run listing)')
    args = parser.parse_args()

    campaign = args.campaign.strip()
    if not campaign:
        from pcs.models import Campaign
        current = Campaign.objects.filter(lifecycle='current').first()
        if current is None:
            print('no current campaign', file=sys.stderr)
            return 2
        campaign = current.name

    result = derive_expected_events(campaign)
    proposals = result['proposals']
    by_rule = collections.defaultdict(list)
    for proposal in proposals:
        by_rule[proposal['rule']].append(proposal)

    print(f'campaign {campaign}: {len(proposals)} derived target(s) proposed')
    for rule in sorted(by_rule):
        rows = by_rule[rule]
        print(f'  {rule} {RULE_TITLES[rule]}: {len(rows)} — '
              f'{sum(r["value"] for r in rows):,} target events')
        for row in rows[:args.show]:
            print(f'    {row["pc"]:8s} {row["value"]:>12,}  '
                  f'delivered {row["delivered"]:>12,}  {row["name"]}')
        if len(rows) > args.show:
            print(f'    ... {len(rows) - args.show} more')
    for reason, count in sorted(result['skipped'].items()):
        print(f'  skipped: {count} {reason}')
    if result['implausible']:
        print(f'  implausible (>= {IMPLAUSIBLE_TARGET:,}), to clear: '
              f'{len(result["implausible"])}')
        for row in result['implausible']:
            print(f'    {row["pc"]:8s} {row["value"]:>14,} '
                  f'{row["source"]:9s} {row["name"]}')

    if not args.apply:
        print('dry run; --apply writes through the expected-events service')
        return 0

    from pcs.services import ServiceError, dataset_expected_events_set

    for rule in sorted(by_rule):
        rows = by_rule[rule]
        entries = [{'name': r['name'], 'expected_events': r['value'],
                    'source': 'derived'} for r in rows]
        comment = (f'{rule} {RULE_TITLES[rule]} '
                   f'(scripts/derive_expected_events.py); '
                   f'{rows[0]["evidence"]}'
                   + (f' and {len(rows) - 1} more' if len(rows) > 1 else ''))
        try:
            outcome = dataset_expected_events_set(
                entries, comment, changed_by=args.changed_by)
        except ServiceError as exc:
            print(f'  {rule}: service error: {exc}', file=sys.stderr)
            return 1
        print(f'  {rule}: changed {len(outcome["changed"])}, '
              f'unchanged {len(outcome["unchanged"])}, '
              f'unknown {len(outcome["unknown"])}')
        if outcome['unknown']:
            print(f'    unknown names: {outcome["unknown"][:5]}',
                  file=sys.stderr)
    if result['implausible']:
        entries = [{'name': r['name'], 'expected_events': None, 'source': ''}
                   for r in result['implausible']]
        comment = (f'cleared: recorded target at or above '
                   f'{IMPLAUSIBLE_TARGET:,} events is not a sample size '
                   f'(scripts/derive_expected_events.py)')
        try:
            outcome = dataset_expected_events_set(
                entries, comment, changed_by=args.changed_by)
        except ServiceError as exc:
            print(f'  clear: service error: {exc}', file=sys.stderr)
            return 1
        print(f'  cleared implausible: {len(outcome["changed"])}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
