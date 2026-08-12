#!/usr/bin/env python3
"""fold_slug_rows.py — retract the 2026-08-12 slug-sample scheme.

The sample axis is physics vocabulary. This script removes every
mechanical slug sample (12-hex, with or without ``src-``) by the
model's own identity law (``pcs.physics_config``, the single
authority):

Per campaign, slug rows group by their sample-less configuration key.
Each group resolves to one edition — the existing non-slug edition
where one exists, else the earliest slug row promoted (its sample
becomes the authority-resolved discriminator: the path-derived
single-particle angle where one exists, else empty). Every other slug
row in the group is a physical record of the same configuration: its
outputs entries move to the edition's identity task (creating them
from the row's recorded payload where it has no task), tasks carrying
PanDA associations re-point to the edition, emptied bookkeeping tasks
delete, and the row deletes. Groups whose promoted name would collide
with an unrelated row are left untouched and reported as conflicts
for curation. Campaign target events recorded on a folded row carry
to the edition where the edition has none.

After the fold: output ownership consolidates per campaign
(``consolidate_output_ownership``), and physics configurations left
with no editions delete.

Dry-run by default, printing the plan and writing a full audit JSON
(every row's payload before any change); ``--apply`` executes.
Django-bootstrap standalone script — also usable by hand.

Usage::

    cd /data/wenauseic/github/swf-monitor/src
    source ../../swf-testbed/.venv/bin/activate && source ~/.env
    python ../../swf-epicprod/scripts/fold_slug_rows.py \
        [--campaign NAME] [--apply]
"""
import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(THIS_DIR, '..', '..', 'swf-monitor', 'src'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402
django.setup()

from django.db import transaction  # noqa: E402

from pcs.models import Campaign, Dataset, PandaTasks, PhysicsConfig, \
    ProdTask  # noqa: E402
from pcs.physics_config import physics_config_key  # noqa: E402
from pcs.reconcile import _identity_task, _upsert_task_output  # noqa: E402

SLUG_RE = re.compile(r'^(src-)?[0-9a-f]{12}$')
AUDIT_DIR = '/data/wenauseic/swf-delivery'


def sampleless_detail(row):
    """The authority's resolution of the row with its slug removed."""
    held = row.sample_name
    row.sample_name = ''
    try:
        return physics_config_key(row)
    finally:
        row.sample_name = held


def row_payload(row):
    """Full recoverable record of a row for the audit file."""
    return {
        'pk': row.pk, 'dataset_name': row.dataset_name,
        'composed_name': row.composed_name, 'scope': row.scope,
        'did': row.did, 'campaign': row.campaign.name if row.campaign_id
        else '', 'sample_name': row.sample_name,
        'detector_version': row.detector_version,
        'detector_config': row.detector_config,
        'block_num': row.block_num,
        'tags': {
            'physics': row.physics_tag.tag_label if row.physics_tag_id
            else '', 'evgen': row.evgen_tag.tag_label if row.evgen_tag_id
            else '', 'simu': row.simu_tag.tag_label if row.simu_tag_id
            else '', 'reco': row.reco_tag.tag_label if row.reco_tag_id
            else '', 'background': row.background_tag.tag_label
            if row.background_tag_id else '',
        },
        'file_count': row.file_count, 'data_size': row.data_size,
        'expected_events': row.expected_events,
        'created_by': row.created_by, 'metadata': row.metadata,
    }


def outputs_from_row(row):
    """The row's outputs entries: its tasks' recorded entries, else one
    constructed from the row's own payload."""
    entries = []
    for task in ProdTask.objects.filter(dataset=row):
        entries.extend((task.overrides or {}).get('outputs') or [])
    if entries:
        return entries
    meta = row.metadata or {}
    location = (meta.get('source') or {}).get('location', '')
    past = meta.get('past_output') or {}
    if not location:
        return []
    return [{
        'did': location,
        'stage': past.get('stage', meta.get('stage', '')),
        'version': past.get('version', ''),
        'filters': past.get('filters', {}),
        'rses': [{'rse': r.get('name', ''), 'files': r.get('files', 0),
                  'total': r.get('total', 0),
                  'complete': r.get('status') == 'complete'}
                 for r in past.get('rses') or []],
        'file_count': row.file_count,
        'bytes': row.data_size,
        'complete': bool(past.get('complete')),
        'checked_at': datetime.now().isoformat(),
    }]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--campaign', default='')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()

    campaigns = Campaign.objects.all()
    if args.campaign:
        campaigns = campaigns.filter(name=args.campaign)

    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    audit = {'stamp': stamp, 'apply': args.apply, 'campaigns': {}}
    totals = defaultdict(int)

    for campaign in campaigns.order_by('name'):
        rows = list(Dataset.objects.filter(campaign=campaign)
                    .select_related('physics_tag', 'evgen_tag', 'simu_tag',
                                    'reco_tag', 'background_tag',
                                    'campaign'))
        slug_rows = [r for r in rows if SLUG_RE.match(r.sample_name or '')]
        if not slug_rows:
            continue
        keep_rows = [r for r in rows if not SLUG_RE.match(r.sample_name or '')]
        taken_names = {
            r.composed_name for r in
            Dataset.objects.exclude(campaign=campaign)} | {
            r.composed_name for r in keep_rows}

        groups = defaultdict(lambda: {'edition': None, 'slugs': []})
        for r in keep_rows:
            try:
                key = physics_config_key(r)['key']
            except Exception as exc:  # noqa: BLE001
                print(f'WARNING: key failed for kept row {r.pk}: {exc}',
                      file=sys.stderr)
                continue
            if groups[key]['edition'] is None:
                groups[key]['edition'] = r
        for r in slug_rows:
            detail = sampleless_detail(r)
            groups[detail['key']]['slugs'].append((r, detail['sample']))

        plan = {'promote': [], 'fold': [], 'conflict': [], 'carry': []}
        for key, group in groups.items():
            if not group['slugs']:
                continue
            edition = group['edition']
            slugs = sorted(group['slugs'], key=lambda pair: pair[0].pk)
            promoted = None
            if edition is None:
                promoted, resolved_sample = slugs[0]
                suffix = f'.{promoted.sample_name}'
                base = promoted.composed_name
                if base.endswith(suffix):
                    base = base[:-len(suffix)]
                candidate = f'{base}.{resolved_sample}' if resolved_sample \
                    else base
                if candidate in taken_names:
                    plan['conflict'].append({
                        'rows': [row_payload(r) for r, _ in slugs],
                        'candidate': candidate,
                        'reason': 'promoted name collides outside group'})
                    continue
                taken_names.add(candidate)
                edition = promoted
                plan['promote'].append({
                    'pk': promoted.pk, 'from': promoted.composed_name,
                    'sample': resolved_sample, 'to': candidate})
                slugs = slugs[1:]
            for r, _ in slugs:
                entry = {'row': row_payload(r),
                         'edition_pk': edition.pk,
                         'edition': edition.composed_name,
                         'outputs_moved': len(outputs_from_row(r))}
                if (r.expected_events is not None
                        and edition.expected_events is None):
                    entry['carry_expected'] = r.expected_events
                    plan['carry'].append({'edition_pk': edition.pk,
                                          'expected': r.expected_events})
                plan['fold'].append(entry)

        audit['campaigns'][campaign.name] = plan
        for cls in plan:
            totals[cls] += len(plan[cls])

        if not args.apply:
            continue

        with transaction.atomic():
            promoted_pks = {p['pk']: p for p in plan['promote']}
            for pk, p in promoted_pks.items():
                row = Dataset.objects.get(pk=pk)
                row.sample_name = p['sample']
                row.save()
            for entry in plan['fold']:
                row = Dataset.objects.filter(pk=entry['row']['pk']).first()
                if row is None:
                    continue
                edition = Dataset.objects.get(pk=entry['edition_pk'])
                task = _identity_task(edition.composed_name)
                for output in outputs_from_row(row):
                    if task is not None:
                        _upsert_task_output(task, output)
                if entry.get('carry_expected') is not None \
                        and edition.expected_events is None:
                    # The target carries verbatim, tier and all — the
                    # folded row's curation is the edition's curation.
                    edition.expected_events = entry['carry_expected']
                    edition.expected_events_source = \
                        row.expected_events_source
                    edition.save(update_fields=['expected_events',
                                                'expected_events_source'])
                for rtask in ProdTask.objects.filter(dataset=row):
                    if PandaTasks.objects.filter(prod_task=rtask).exists():
                        rtask.dataset = edition
                        rtask.save(update_fields=['dataset'])
                    else:
                        rtask.delete()
                row.delete()
            from pcs.services import consolidate_output_ownership
            consolidate_output_ownership(campaign)

    if args.apply:
        orphans = PhysicsConfig.objects.filter(editions__isnull=True)
        totals['orphan_pcs_deleted'] = orphans.count()
        orphans.delete()

    os.makedirs(AUDIT_DIR, exist_ok=True)
    audit_path = os.path.join(AUDIT_DIR, f'slug_fold_audit_{stamp}.json')
    with open(audit_path, 'w') as fh:
        json.dump(audit, fh, indent=1, default=str)
    print('TOTALS ' + json.dumps(dict(totals)))
    print(f'audit: {audit_path}')
    print('mode: ' + ('APPLIED' if args.apply else 'dry-run'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
