"""Move PanDA attempt records to the task whose inputs they ran.

The input matcher fanned the unversioned 18x275 DIS NC records out to
the versioned pythia8.316 Q² ranges until 2026-10-01
(EPICPROD_EVGEN_INPUTS.md, Matching), and two PCS submissions built
their manifests from that match: 40440, recorded on pc6496 (minQ2=10),
ran the versioned q2_10to100 and q2_100to1000 files; 40443, recorded on
pc6497 (minQ2=100), ran the versioned q2_100to1000 files. Their outputs
belong to the versioned configurations, pc434 and pc433 (Sakib,
epic-prod-ops 2026-10-06).

Each move re-points the ``PandaTasks`` row to the target task, records
the move on the row (``metadata.rehomed``), resets each task's
``panda_task_id`` to its newest remaining attempt, logs one
``panda_attempt_rehome`` action, and re-settles output ownership for
the campaign (``consolidate_output_ownership``, which reads the moved
rows' ``output_datasets``). The PanDA association sweep finds an
attempt by its jediTaskID first, so it keeps the new home.

Run under the venv with the swf-monitor project on the path:

    cd <swf-monitor>/src && source ~/.env
    <venv>/bin/python <swf-epicprod>/scripts/rehome_panda_attempts.py [--apply]

Dry-run by default; --apply writes.
"""

import argparse
import os
import sys

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402

django.setup()

from django.db import transaction  # noqa: E402
from django.utils import timezone  # noqa: E402

from monitor_app.epicprod_logging import log_epicprod_action  # noqa: E402
from pcs.models import PandaTasks, ProdTask  # noqa: E402
from pcs.services import consolidate_output_ownership  # noqa: E402

REASON = ('ran the versioned pythia8.316 inputs of this configuration; '
          'recorded on an unversioned minQ2 configuration by the pre-10/1 '
          'input match (Sakib, epic-prod-ops 2026-10-06)')

# jediTaskID -> target ProdTask pk
MOVES = {
    40440: 8391,  # pc434, group.EIC.26.07.1.epic_craterlake.p2223.e49.s9.r9 (q2_10to100)
    40443: 8390,  # pc433, group.EIC.26.07.1.epic_craterlake.p2222.e49.s9.r9 (q2_100to1000)
}


def newest_attempt(task):
    row = (PandaTasks.objects.filter(prod_task=task, jedi_task_id__isnull=False)
           .order_by('-try_number', '-pk').first())
    return row.jedi_task_id if row else None


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--user', default='wenaus')
    args = parser.parse_args()

    plan = []
    for jedi, target_pk in MOVES.items():
        row = PandaTasks.objects.select_related('prod_task').get(jedi_task_id=jedi)
        target = ProdTask.objects.get(pk=target_pk)
        source = row.prod_task
        if source.pk == target.pk:
            print(f'{jedi}: already on {target.name}')
            continue
        if source.campaign_id != target.campaign_id:
            sys.exit(f'{jedi}: {source.name} and {target.name} are in different campaigns')
        clash = PandaTasks.objects.filter(prod_task=target, try_number=row.try_number).exclude(pk=row.pk)
        if clash.exists():
            sys.exit(f'{jedi}: {target.name} already has a try {row.try_number}')
        plan.append((row, source, target))
        print(f'{jedi} (try {row.try_number}, {row.task_name}): {source.name} -> {target.name}')

    if not plan:
        print('nothing to move')
        return
    if not args.apply:
        print('dry run; --apply to write')
        return

    campaigns = set()
    with transaction.atomic():
        for row, source, target in plan:
            meta = dict(row.metadata or {})
            meta['rehomed'] = {'from': source.name, 'to': target.name,
                               'at': timezone.now().isoformat(),
                               'by': args.user, 'reason': REASON}
            row.prod_task = target
            row.metadata = meta
            row.save(update_fields=['prod_task', 'metadata', 'updated_at'])
            for task in (source, target):
                task.refresh_from_db()
                pointer = newest_attempt(task)
                if task.panda_task_id != pointer:
                    task.panda_task_id = pointer
                    task.save(update_fields=['panda_task_id'])
            campaigns.add(target.campaign)
            log_epicprod_action(
                'web', 'panda_attempt_rehome',
                subject_type='prod_task', subject_key=target.name,
                username=args.user, sublevel='normal', live_default=True,
                message=(f'PanDA attempt {row.jedi_task_id} ({row.task_name}) '
                         f'moved from {source.name} to {target.name}: {REASON}'),
                jedi_task_id=row.jedi_task_id, source=source.name,
                target=target.name, reason=REASON)
    for campaign in campaigns:
        consolidate_output_ownership(campaign)
        print(f'output ownership re-settled for {campaign.name}')
    for row, source, target in plan:
        for task in (source, target):
            task.refresh_from_db()
            outs = [o.get('did') for o in task.outputs]
            print(f'{task.name}: panda_task_id {task.panda_task_id}; owns {outs}')


if __name__ == '__main__':
    main()
