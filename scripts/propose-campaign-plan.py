"""Generate the campaign-assembly proposal set
(CONTINUOUS_PRODUCTION.md, Campaign assembly).

Derives one disposition proposal per physics configuration of the
source campaign (``pcs.assembly.build_assembly_items`` — defaults in
the evidence tiers, code-filled facts) and, with --apply, submits them
through the AI proposal subsystem for review on the AI proposals page
and the campaign plan page. The target campaign row is created on
first apply (lifecycle ``future``). Dry-run default; re-running is the
heartbeat (pending proposals are superseded, decided ones respected).

Run under the venv with the swf-monitor project on the path:

    cd <swf-monitor>/src && source <venv>/bin/activate && source ~/.env
    python <swf-epicprod>/scripts/propose-campaign-plan.py \\
        --source 26.07 --target 26.09 [--apply] [--created-by <user>]
"""

import argparse
import collections
import os

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402

django.setup()

from pcs.assembly import build_assembly_items, propose_campaign_assembly  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', default='26.07')
    parser.add_argument('--target', default='26.09')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--created-by', default='campaign-assembly')
    parser.add_argument('--show', type=int, default=6,
                        help='sample items to print per disposition')
    args = parser.parse_args()

    built = build_assembly_items(args.source)
    by_disposition = collections.defaultdict(list)
    for item in built['items']:
        by_disposition[item['disposition']].append(item)
    print(f"{args.source} -> {args.target}: {len(built['items'])} "
          f"configurations")
    for disposition in ('include', 'defer', 'retire'):
        rows = by_disposition.get(disposition, [])
        print(f"\n{disposition}: {len(rows)}")
        for item in rows[:args.show]:
            target = item['target_events']
            print(f"  {item['pc']}: "
                  + (f"target {target:,} " if target else '')
                  + (f"priority {item['priority']} "
                     if item['priority'] is not None else '')
                  + f"— {item['evidence']}")
        if len(rows) > args.show:
            print(f"  ... and {len(rows) - args.show} more")

    if not args.apply:
        print('\ndry run; use --apply to submit the proposal set')
        return
    import datetime
    batch = f'assembly-{args.target}-{datetime.date.today().isoformat()}'
    result = propose_campaign_assembly(
        args.source, args.target,
        created_by=args.created_by, batch_id=batch)
    print(f"\nproposed {len(result['proposed'])}, "
          f"noop {len(result['noop'])}, "
          f"denied-skipped {len(result['denied'])}, "
          f"invalid {len(result['invalid'])} [batch {batch}]")
    for line in result['invalid'][:10]:
        print(f'  invalid: {line}')


if __name__ == '__main__':
    main()
