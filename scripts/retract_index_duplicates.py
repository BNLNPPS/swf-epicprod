#!/usr/bin/env python3
"""retract_index_duplicates.py — the past-output editions the epic-prod index
import made for outputs PCS created at submission.

PCS records each output dataset it creates at submission on the PanDA
attempt (``PandaTasks.metadata.output_datasets``), and that record makes the
dataset its task's output (``services.precreated_output_owners``). Before
2026-10-18 the epic-prod index import did not read it, and gave 34 such
outputs a second identity: a past-output edition on the generator-unrecorded
tag e52, with a past_output task carrying the same output. Two were the eAg
outputs of tasks 40846 and 40847, whose species the path parser did not
know, so the import minted p2480 and p2481: DIS 9x115 with no species.

The repair:

1. every past-output edition whose source DID a PCS task created, and that
   is not that task's own edition, is deleted with its past_output task.
   The owning task receives the outputs entry when it lacks it. A task that
   is not past_output, or that anything refers to (jobs, files, PanDA
   attempts, delivered outputs, an anchored request), refuses the run and
   nothing is written;
2. a physics configuration the deletion leaves with no edition is superseded
   by the owning edition's configuration, and a physics tag it leaves with
   no edition is superseded by the owner's tag when the two differ only by
   the ion species the parser dropped (PCS_PERMANENT_IDENTITIES.md);
3. output ownership is consolidated per campaign and one
   ``index_duplicate_retract`` action records the run.

Dry run by default; ``--apply`` writes. The audit file records every
deleted row in full under /data/wenauseic/swf-delivery/.

    cd /data/wenauseic/github/swf-monitor/src
    ../../swf-testbed/.venv/bin/python ../../swf-epicprod/scripts/retract_index_duplicates.py [--apply]
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone as dt_timezone

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.environ.get('SWF_MONITOR_SRC',
                                  os.path.join(THIS_DIR, '..', '..', 'swf-monitor', 'src')))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402
django.setup()

from django.db import transaction  # noqa: E402

from monitor_app.models import EpicProdFile, EpicProdJob  # noqa: E402
from pcs.models import (Campaign, Dataset, DeliveredOutput, PandaTasks, PhysicsConfig,  # noqa: E402
                        PhysicsTag, ProdRequest, ProdTask)
from pcs.permanence import identity_change, set_lifecycle  # noqa: E402
from pcs.reconcile import _upsert_task_output  # noqa: E402
from pcs.services import _physics_key, consolidate_output_ownership, precreated_output_owners  # noqa: E402

AUDIT_DIR = os.environ.get('SWF_DELIVERY_DIR', '/data/wenauseic/swf-delivery')


def row_payload(row):
    """Full recoverable record of a row for the audit file."""
    return {
        'pk': row.pk, 'dataset_name': row.dataset_name,
        'composed_name': row.composed_name, 'scope': row.scope, 'did': row.did,
        'campaign': row.campaign.name if row.campaign_id else '',
        'sample_name': row.sample_name, 'detector_version': row.detector_version,
        'detector_config': row.detector_config, 'block_num': row.block_num,
        'tags': {k: (getattr(row, f'{k}_tag').tag_label if getattr(row, f'{k}_tag_id') else '')
                 for k in ('physics', 'evgen', 'simu', 'reco', 'background')},
        'physics_config': row.physics_config.label if row.physics_config_id else '',
        'file_count': row.file_count, 'data_size': row.data_size,
        'expected_events': row.expected_events, 'created_by': row.created_by,
        'created_at': row.created_at.isoformat() if row.created_at else '',
        'metadata': row.metadata,
        'tasks': [{'pk': t.pk, 'name': t.name, 'status': t.status, 'overrides': t.overrides}
                  for t in ProdTask.objects.filter(dataset=row)],
    }


def species_dropped(wrong, right):
    """Whether tag ``wrong`` is ``right`` with its ion species lost."""
    p = dict(right.parameters or {})
    if not p.get('beam_species') or (wrong.parameters or {}).get('beam_species'):
        return False
    p.pop('beam_species')
    p.pop('nucleon', None)
    return _physics_key(wrong.parameters) == _physics_key(p)


def plan_campaign(campaign, plan):
    owners = precreated_output_owners(campaign)
    for row in (Dataset.objects.filter(campaign=campaign, metadata__has_key='past_output')
                .select_related('physics_tag', 'physics_config', 'campaign')):
        did = ((row.metadata or {}).get('source') or {}).get('location', '')
        owner_name = owners.get(did)
        if not owner_name:
            continue
        owner = ProdTask.objects.filter(name=owner_name).select_related(
            'dataset__physics_tag', 'dataset__physics_config').first()
        if owner is None:
            plan['refused'].append({'row': row.composed_name, 'reason': f'owner {owner_name} not found'})
            continue
        if owner.dataset_id == row.pk:
            continue
        tasks = list(ProdTask.objects.filter(dataset=row))
        reasons = []
        for t in tasks:
            if t.status != 'past_output':
                reasons.append(f'task {t.name} is {t.status}')
            for model, label in ((EpicProdJob, 'jobs'), (EpicProdFile, 'files'),
                                 (PandaTasks, 'PanDA attempts'), (DeliveredOutput, 'delivered outputs')):
                if model.objects.filter(prod_task=t).exists():
                    reasons.append(f'task {t.name} has {label}')
        if ProdRequest.objects.filter(data__physics_config_anchor=row.composed_name).exists():
            reasons.append('a request anchors on it')
        if reasons:
            plan['refused'].append({'row': row.composed_name, 'reason': '; '.join(reasons)})
            continue
        carried = any(o.get('did') == did for o in (owner.overrides or {}).get('outputs') or [])
        plan['retract'].append({
            'row': row.pk, 'composed_name': row.composed_name, 'did': did,
            'owner': owner.name, 'owner_edition': owner.dataset.composed_name,
            'owner_carries_output': carried,
            'physics_tag': row.physics_tag.tag_label if row.physics_tag_id else '',
            'owner_physics_tag': (owner.dataset.physics_tag.tag_label
                                  if owner.dataset.physics_tag_id else ''),
            'physics_config': row.physics_config.label if row.physics_config_id else '',
            'owner_physics_config': (owner.dataset.physics_config.label
                                     if owner.dataset.physics_config_id else ''),
            'campaign': campaign.pk,
        })


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--apply', action='store_true', help='write; default reports only')
    ap.add_argument('--changed-by', default='retract_index_duplicates')
    args = ap.parse_args()
    now = datetime.now(dt_timezone.utc).isoformat(timespec='seconds')
    plan = {'retract': [], 'refused': [], 'supersede_configs': [], 'supersede_tags': []}

    campaigns = Campaign.objects.filter(
        pk__in=Dataset.objects.filter(metadata__has_key='past_output')
        .values_list('campaign_id', flat=True).distinct())
    for campaign in campaigns:
        plan_campaign(campaign, plan)

    retracted = {r['row'] for r in plan['retract']}
    for label, owner_label in {(r['physics_config'], r['owner_physics_config'])
                               for r in plan['retract'] if r['physics_config']}:
        if label == owner_label or not owner_label:
            continue
        if Dataset.objects.filter(physics_config__label=label).exclude(pk__in=retracted).exists():
            continue
        plan['supersede_configs'].append({'config': label, 'by': owner_label})
    for label, owner_label in {(r['physics_tag'], r['owner_physics_tag'])
                               for r in plan['retract'] if r['physics_tag']}:
        if label == owner_label or not owner_label:
            continue
        if Dataset.objects.filter(physics_tag__tag_label=label).exclude(pk__in=retracted).exists():
            continue
        wrong = PhysicsTag.objects.get(tag_label=label)
        right = PhysicsTag.objects.get(tag_label=owner_label)
        if species_dropped(wrong, right):
            plan['supersede_tags'].append({'tag': label, 'by': owner_label})
        else:
            plan.setdefault('tags_left_unreferenced', []).append(label)

    summary = {k: len(v) for k, v in plan.items()}
    print(json.dumps({'applied': False, **summary}))
    for r in plan['retract']:
        carry = '' if r['owner_carries_output'] else ' (output moves to the owner)'
        print(f"  retract {r['composed_name']} -> {r['owner']}{carry}")
    for c in plan['supersede_configs']:
        print(f"  supersede {c['config']} by {c['by']}")
    for t in plan['supersede_tags']:
        print(f"  supersede {t['tag']} by {t['by']}")
    for label in plan.get('tags_left_unreferenced', []):
        print(f"  left unreferenced: {label}")
    for r in plan['refused']:
        print(f"  REFUSE {r['row']}: {r['reason']}")
    if plan['refused']:
        print('refused rows: nothing is written')
        return 1
    if not args.apply:
        return 0

    from monitor_app.epicprod_logging import log_epicprod_action
    reason = ('past-output edition the epic-prod index import made for an output '
              'PCS created at submission (retract_index_duplicates.py)')
    audit_rows = []
    with identity_change(args.changed_by, reason):
        for r in plan['retract']:
            row = Dataset.objects.select_related(
                'physics_tag', 'evgen_tag', 'simu_tag', 'reco_tag', 'background_tag',
                'physics_config', 'campaign').get(pk=r['row'])
            audit_rows.append(row_payload(row))
            if not r['owner_carries_output']:
                owner = ProdTask.objects.get(name=r['owner'])
                for task in ProdTask.objects.filter(dataset=row):
                    for entry in (task.overrides or {}).get('outputs') or []:
                        if entry.get('did') == r['did']:
                            _upsert_task_output(owner, entry)
            for task in ProdTask.objects.filter(dataset=row):
                task.delete()
            row.delete()
        for c in plan['supersede_configs']:
            set_lifecycle(PhysicsConfig.objects.get(label=c['config']), 'superseded',
                          reason=f"its only editions were index duplicates of {c['by']}'s outputs",
                          changed_by=args.changed_by,
                          replacement=PhysicsConfig.objects.get(label=c['by']))
        for t in plan['supersede_tags']:
            set_lifecycle(PhysicsTag.objects.get(tag_label=t['tag']), 'superseded',
                          reason=f"{t['by']} with the ion species the path parser dropped",
                          changed_by=args.changed_by,
                          replacement=PhysicsTag.objects.get(tag_label=t['by']))
        with transaction.atomic():
            for cid in {r['campaign'] for r in plan['retract']}:
                consolidate_output_ownership(Campaign.objects.get(pk=cid))

    os.makedirs(AUDIT_DIR, exist_ok=True)
    audit_path = os.path.join(AUDIT_DIR, f"index_duplicate_retract_{now.replace(':', '')}.json")
    with open(audit_path, 'w') as fh:
        json.dump({'at': now, 'by': args.changed_by, 'summary': summary, 'plan': plan,
                   'rows': audit_rows}, fh, indent=1, default=str)
    log_epicprod_action(
        'pcs', 'index_duplicate_retract', outcome='ok', sublevel='low', live_default=False,
        subject_type='dataset', subject_key=f"{summary['retract']} editions",
        username=args.changed_by,
        message=(f"index_duplicate_retract: {summary['retract']} past-output edition(s) of "
                 f"PCS-created outputs deleted with their tasks; configurations superseded: "
                 f"{', '.join(c['config'] for c in plan['supersede_configs']) or 'none'}; "
                 f"tags superseded: {', '.join(t['tag'] for t in plan['supersede_tags']) or 'none'}; "
                 f"audit {audit_path}"),
        retracted=summary['retract'], configs=summary['supersede_configs'],
        tags=summary['supersede_tags'])
    print(json.dumps({'applied': True, **summary, 'audit': audit_path}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
