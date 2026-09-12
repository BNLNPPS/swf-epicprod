#!/usr/bin/env python3
"""fold_duplicate_tag_editions.py — one edition per identity where a physics
tag was made twice.

A physics tag is matched on its full parameter set, so two tags with the
same parameters are one physics tagged twice; the editions on the later
tag are the same identities as the editions on the earlier one, under
another name. The 26.07 BeAGLE eHe3 intakes of 2026-08-19 made twelve such
tags (the species dropped from the task name by the remainder split; fixed
in pcs/physics_match.py), and the Rucio reconcile, finding no edition on
the tags that had the species, made a past-ingest placeholder for every
produced dataset: fifteen identities, each with an edition carrying the
PanDA task on the wrong tag and a placeholder carrying the output on the
right one.

The repair applies the rule of PCS_COMPOSED_NAME_INTEGRITY.md (the slug
retraction): a physical delivery of a configuration that already has its
edition is not an identity row. For every edition on a duplicate tag:

1. its twins are folded into it: the editions of the same campaign
   already on the earlier tag with the same generator identity (read
   from their paths, since a placeholder may wear the unrecorded evgen
   sentinel) and the same sample, and no PanDA association of their own.
   A twin's outputs entries move to the edition's identity task, its
   campaign target carries when the edition has none, its rows delete;
2. an edition holding the name the edition will take that is not a twin
   is another identity: when its bound evgen tag disagrees with its path
   (the 26.07.1 BeAGLE 3.0 outputs wore the 3.1 tag) it takes the tag the
   path names, minted when absent, which frees the name; otherwise the
   pair is refused and nothing is written;
3. the edition then takes the earlier tag; its composed name and physics
   configuration follow on save, with the change in ``metadata['rebind']``.

Then output ownership is consolidated per campaign and one ``edition_fold``
action records the run. Every issued physics configuration remains permanent,
including those left without editions. The duplicate tags are left in place, unreferenced, for the operator.

Dry run by default; ``--apply`` writes. The audit file records every
folded row in full under /data/wenauseic/swf-delivery/.

    cd /data/wenauseic/github/swf-monitor/src
    ../../swf-testbed/.venv/bin/python ../../swf-epicprod/scripts/fold_duplicate_tag_editions.py [--apply] [--tag WRONG ...]
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

from pcs.models import Campaign, Dataset, PandaTasks, PhysicsTag, ProdRequest, ProdTask  # noqa: E402
from pcs.physics_config import _source_path, evgen_identity  # noqa: E402
from pcs.physics_match import derive_evgen  # noqa: E402
from pcs.reconcile import _identity_task, _upsert_task_output  # noqa: E402
from pcs.services import _physics_key, consolidate_output_ownership, find_or_create_evgen_tag  # noqa: E402

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
        'file_count': row.file_count, 'data_size': row.data_size,
        'expected_events': row.expected_events, 'created_by': row.created_by,
        'metadata': row.metadata,
        'tasks': [{'name': t.name, 'status': t.status, 'outputs': (t.overrides or {}).get('outputs') or []}
                  for t in ProdTask.objects.filter(dataset=row)],
    }


def outputs_from_row(row):
    """The row's outputs entries: its tasks' recorded entries, else one
    built from the row's own past-output record."""
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
        'did': location, 'stage': past.get('stage', meta.get('stage', '')),
        'version': past.get('version', ''), 'filters': past.get('filters', {}),
        'rses': [{'rse': r.get('name', ''), 'files': r.get('files', 0), 'total': r.get('total', 0),
                  'complete': r.get('status') == 'complete'} for r in past.get('rses') or []],
        'file_count': row.file_count, 'bytes': row.data_size,
        'complete': bool(past.get('complete')),
        'checked_at': datetime.now(dt_timezone.utc).isoformat(timespec='seconds'),
    }]


def duplicate_tags(labels=None):
    """(wrong, right) pairs: a tag whose parameters equal an earlier tag's
    (the locked one first, else the lowest number)."""
    by_key = {}
    for tag in PhysicsTag.objects.all().order_by('tag_number'):
        by_key.setdefault(_physics_key(tag.parameters), []).append(tag)
    pairs = []
    for tags in by_key.values():
        if len(tags) < 2:
            continue
        ordered = sorted(tags, key=lambda t: (0 if t.status == 'locked' else 1, t.tag_number))
        right = ordered[0]
        for wrong in ordered[1:]:
            if labels and wrong.tag_label not in labels:
                continue
            pairs.append((wrong, right))
    return pairs


def name_with_tag(edition, tag):
    return edition.composed_name.replace(f'.{edition.physics_tag.tag_label}.', f'.{tag.tag_label}.', 1)


def evgen_from_path(edition):
    """The evgen parameters the edition's path names, or None."""
    path = _source_path(edition)
    return derive_evgen(path, '') if path else None


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--apply', action='store_true', help='write; default reports only')
    ap.add_argument('--tag', action='append', default=[], help='only these duplicate tags (repeatable)')
    ap.add_argument('--changed-by', default='fold_duplicate_tag_editions')
    args = ap.parse_args()
    now = datetime.now(dt_timezone.utc).isoformat(timespec='seconds')
    plan = {'evgen_rebinds': [], 'folds': [], 'rebinds': [], 'refused': []}
    pairs = duplicate_tags(set(args.tag) or None)
    campaigns = set()

    for wrong, right in pairs:
        for edition in Dataset.objects.filter(physics_tag=wrong).order_by('pk'):
            campaigns.add(edition.campaign_id)
            new_name = name_with_tag(edition, right)
            e_ident, _prov = evgen_identity(edition)
            entry = {'edition': edition.pk, 'name': edition.composed_name, 'new_name': new_name,
                     'wrong': wrong.tag_label, 'right': right.tag_label}
            # The twins: editions of the same campaign already on the right
            # tag with the same generator identity (from their paths, since
            # a placeholder may wear the unrecorded sentinel) and the same
            # sample, carrying no PanDA association of their own.
            candidates = (Dataset.objects.filter(detector_version=edition.detector_version,
                                                 physics_tag=right, sample_name=edition.sample_name,
                                                 background_tag_id=edition.background_tag_id)
                          .exclude(pk=edition.pk).order_by('pk'))
            refused = False
            for other in candidates:
                o_ident, o_prov = evgen_identity(other)
                held_by_panda = PandaTasks.objects.filter(prod_task__dataset=other).exists()
                if o_ident == e_ident and not held_by_panda:
                    plan['folds'].append({'edition': edition.pk, 'name': edition.composed_name,
                                          'holder': other.pk, 'holder_name': other.composed_name,
                                          'outputs_moved': len(outputs_from_row(other)),
                                          'row': row_payload(other)})
                elif other.composed_name == new_name:
                    if o_prov == 'path-over-tag':
                        # The holder of the name wears an evgen tag its
                        # path denies: it takes the path's tag, which frees
                        # the name; it is another identity, not a twin.
                        plan['evgen_rebinds'].append({'holder': other.pk, 'name': other.composed_name,
                                                      'from': other.evgen_tag.tag_label,
                                                      'to': evgen_from_path(other)})
                    else:
                        plan['refused'].append(dict(entry, holder=other.pk, reason=(
                            'the holder has a PanDA association of its own' if held_by_panda
                            else f'generator identity differs: {o_ident} vs {e_ident}')))
                        refused = True
            if refused:
                continue
            plan['rebinds'].append(entry)

    summary = {k: len(v) for k, v in plan.items()}
    print(json.dumps({'dry_run': not args.apply, 'pairs': [(w.tag_label, r.tag_label) for w, r in pairs],
                      **summary}))
    for e in plan['evgen_rebinds']:
        print(f"  evgen  {e['name']}  {e['from']} -> {e['to']}")
    for f in plan['folds']:
        print(f"  fold   {f['holder_name']} (edition {f['holder']}, {f['outputs_moved']} output(s)) into {f['name']}")
    for r in plan['rebinds']:
        print(f"  rebind {r['name']} -> {r['new_name']}")
    for r in plan['refused']:
        print(f"  REFUSE {r['name']}: {r['reason']}")
    if not args.apply:
        return 0

    from monitor_app.epicprod_logging import log_epicprod_action
    with transaction.atomic():
        for e in plan['evgen_rebinds']:
            holder = Dataset.objects.get(pk=e['holder'])
            tag, _ = find_or_create_evgen_tag(e['to'], created_by=args.changed_by)
            history = list((holder.metadata or {}).get('rebind') or [])
            history.append({'from': {'evgen': holder.evgen_tag.tag_label}, 'to': {'evgen': tag.tag_label},
                            'name_before': holder.composed_name, 'by': args.changed_by, 'at': now,
                            'reason': 'the evgen tag the path names; the bound tag was another version'})
            holder.evgen_tag = tag
            holder.metadata = dict(holder.metadata or {}, rebind=history)
            holder.save()
            e['to_label'] = tag.tag_label
            e['name_after'] = holder.composed_name
        for f in plan['folds']:
            holder = Dataset.objects.get(pk=f['holder'])
            edition = Dataset.objects.get(pk=f['edition'])
            task = _identity_task(edition.composed_name)
            for output in outputs_from_row(holder):
                _upsert_task_output(task, output)
            if holder.expected_events is not None and edition.expected_events is None:
                edition.expected_events = holder.expected_events
                edition.expected_events_source = holder.expected_events_source
            folds = list((edition.metadata or {}).get('fold') or [])
            folds.append({'folded': f['row'], 'by': args.changed_by, 'at': now,
                          'reason': 'a placeholder edition of the same identity on the earlier tag'})
            edition.metadata = dict(edition.metadata or {}, fold=folds)
            edition.save()
            for rtask in ProdTask.objects.filter(dataset=holder):
                rtask.delete()
            holder.delete()
        for r in plan['rebinds']:
            edition = Dataset.objects.get(pk=r['edition'])
            right = PhysicsTag.objects.get(tag_label=r['right'])
            if Dataset.objects.filter(composed_name=r['new_name']).exclude(pk=edition.pk).exists():
                raise RuntimeError(f"{r['new_name']} is still held; nothing written")
            old_name = edition.composed_name
            history = list((edition.metadata or {}).get('rebind') or [])
            history.append({'from': {'physics': r['wrong']}, 'to': {'physics': r['right']},
                            'name_before': old_name, 'by': args.changed_by, 'at': now,
                            'reason': 'the physics tag made twice; the edition takes the earlier one'})
            edition.physics_tag = right
            edition.metadata = dict(edition.metadata or {}, rebind=history)
            edition.save()
            for req in ProdRequest.objects.filter(data__physics_config_anchor=old_name):
                data = dict(req.data or {})
                data['physics_config_anchor'] = edition.composed_name
                req.data = data
                req.save(update_fields=['data'])
            r['pc'] = edition.physics_config.label if edition.physics_config_id else ''
        for cid in campaigns:
            campaign = Campaign.objects.filter(pk=cid).first()
            if campaign is not None:
                consolidate_output_ownership(campaign)
        # Issued configurations remain permanent, including those with no editions.

    os.makedirs(AUDIT_DIR, exist_ok=True)
    audit_path = os.path.join(AUDIT_DIR, f"duplicate_tag_fold_{now.replace(':', '')}.json")
    with open(audit_path, 'w') as fh:
        json.dump({'at': now, 'by': args.changed_by, 'summary': summary, 'plan': plan}, fh, indent=1, default=str)
    unreferenced = [w.tag_label for w, _ in pairs if not Dataset.objects.filter(physics_tag=w).exists()]
    log_epicprod_action(
        'pcs', 'edition_fold', outcome='ok', sublevel='low', live_default=False,
        subject_type='physics_tag', subject_key=','.join(w.tag_label for w, _ in pairs),
        username=args.changed_by,
        message=(f"edition_fold: {summary['folds']} placeholder edition(s) folded, "
                 f"{summary['rebinds']} edition(s) moved to the earlier tag, "
                 f"{summary['evgen_rebinds']} evgen tag(s) corrected from the path, "
                 f"{summary['refused']} refused; tags now unreferenced: {', '.join(unreferenced) or 'none'}; "
                 f"audit {audit_path}"),
        folds=summary['folds'], rebinds=summary['rebinds'], evgen_rebinds=summary['evgen_rebinds'],
        refused=summary['refused'])
    print(json.dumps({'applied': True, **summary, 'unreferenced_tags': unreferenced, 'audit': audit_path}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
