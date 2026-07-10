"""Firsthand Rucio reconciliation (CAMPAIGN_CONTINUUM.md).

Keeps a producing campaign's recorded content current directly from
JLab Rucio — no dependency on the production team's epic-prod
bookkeeping cadence. Runs against the campaign's fetched snapshot
(the deep fetch that already targets producing campaigns), applying
the blessed rules:

- **Update by DID**: a snapshot dataset whose DID the catalog already
  records (``metadata.source.location``) refreshes that row's file
  count, volume, and per-RSE status, and the matching task outputs
  entry.
- **Unknown DIDs resolve to a physics configuration** before anything
  is created: when the configuration matches an existing edition (a
  minted or adopted working row), the dataset joins that identity as a
  physical sibling row and its outputs entry lands on the edition's
  task — never a duplicate edition. Only a configuration with no
  edition at all creates one, past-ingest style.
- **Unresolved physics is reported, never created.**

Scope: producing campaigns outside current/last — campaigns whose
content is ingest-derived. The current campaign's outputs attach
through the request-match machinery; last's records are its CSV
heritage; reconciling either would double-represent editions.
"""
import hashlib
import logging

from django.utils import timezone

from .models import Campaign, Dataset, ProdTask
from .physics_config import group_editions, physics_config_key

_log = logging.getLogger(__name__)


def _replica_summary(rse_replicas):
    """(files, size, rses_block, complete) from snapshot replica records
    — counts are the max across RSEs (identical when all complete), the
    block mirrors the past-ingest shape."""
    files = 0
    size = 0
    rses = []
    complete = bool(rse_replicas)
    for record in rse_replicas or []:
        total = record.get('length') or 0
        avail = record.get('available_length') or 0
        files = max(files, avail)
        size = max(size, record.get('available_bytes')
                   or record.get('bytes') or 0)
        ok = bool(total) and avail >= total
        rses.append({'name': record.get('rse', ''),
                     'files': avail, 'total': total,
                     'status': 'complete' if ok else 'incomplete'})
        complete = complete and ok
    return files, size, rses, complete


def _outputs_entry(did, stage, version, filters, rses, files, size, complete):
    return {
        'did': did,
        'stage': stage,
        'version': version,
        'filters': filters,
        'rses': [{'rse': r['name'], 'files': r['files'],
                  'total': r['total'],
                  'complete': r['status'] == 'complete'} for r in rses],
        'file_count': files,
        'bytes': size,
        'complete': complete,
        'checked_at': timezone.now().isoformat(),
    }


def _identity_task(composed_name):
    """The identity's task, preferring the working one."""
    return (ProdTask.objects.filter(dataset__composed_name=composed_name)
            .exclude(status='past_output').order_by('pk').first()
            or ProdTask.objects.filter(dataset__composed_name=composed_name)
            .order_by('pk').first())


def _upsert_task_output(task, entry):
    """Insert or refresh the task's outputs entry for this DID."""
    if task is None:
        return
    overrides = dict(task.overrides or {})
    outputs = [o for o in (overrides.get('outputs') or [])
               if o.get('did') != entry['did']]
    outputs.append(entry)
    overrides['outputs'] = outputs
    task.overrides = overrides
    task.save(update_fields=['overrides', 'updated_at'])


def reconcile_campaign_from_rucio(campaign_name, *, created_by=''):
    """Reconcile one campaign's catalog records against its fetched
    Rucio snapshot. Returns counts; one ``rucio_reconcile`` action-stream
    event per run. Idempotent: DID-keyed updates, slug-keyed creates.
    """
    from monitor_app.epicprod_logging import log_epicprod_action

    from .physics_match import derive_physics
    from .services import (_decompose_past_did, _ensure_csvimport_anchors,
                           _extract_past_filters, _no_signal_physics_tag,
                           find_or_create_physics_tag, load_rucio_snapshot)

    campaign = Campaign.objects.get(name=campaign_name)
    snapshot = load_rucio_snapshot(campaign_name)
    if not snapshot or not snapshot.get('campaigns'):
        return {'error': f'no Rucio snapshot for {campaign_name}'}

    existing_by_location = {}
    for dataset in Dataset.objects.filter(campaign=campaign):
        location = ((dataset.metadata or {}).get('source') or {}).get('location', '')
        if location:
            existing_by_location[location] = dataset
    heads = list(Dataset.objects.filter(campaign=campaign)
                 .select_related('physics_tag', 'evgen_tag',
                                 'background_tag', 'campaign')
                 .order_by('composed_name', 'block_num', 'pk')
                 .distinct('composed_name'))
    pc_heads = {key: group['editions'][0][0]
                for key, group in group_editions(heads).items()}
    _, anchor_evgen, anchor_simu, anchor_reco, anchor_cfg, _ = \
        _ensure_csvimport_anchors()

    updated, attached, created, unresolved = 0, 0, 0, []
    for path_key, block in (snapshot.get('campaigns') or {}).items():
        segs = [s for s in path_key.split('/') if s]
        if len(segs) < 2:
            continue
        stage, version = segs[0], segs[1]
        for record in block.get('datasets') or []:
            did = record.get('did') or ''
            if not did:
                continue
            files, size, rses, complete = _replica_summary(
                record.get('rse_replicas'))
            filters = _extract_past_filters(did)

            row = existing_by_location.get(did)
            if row is not None:
                row.file_count = files
                row.data_size = size
                metadata = dict(row.metadata or {})
                past = dict(metadata.get('past_output') or {})
                past.update({'rses': rses, 'complete': complete,
                             'stage': stage, 'version': version})
                metadata['past_output'] = past
                row.metadata = metadata
                row.save(update_fields=['file_count', 'data_size', 'metadata'])
                _upsert_task_output(
                    _identity_task(row.composed_name),
                    _outputs_entry(did, stage, version, filters, rses,
                                   files, size, complete))
                updated += 1
                continue

            decomposed = _decompose_past_did(did)
            remainder = decomposed.get('path_remainder', '')
            derived = derive_physics(remainder, beam=filters.get('beam', ''))
            if derived is None:
                unresolved.append(did)
                continue
            if derived.get('process') in ('BEAMGAS', 'SYNRAD'):
                physics_tag = _no_signal_physics_tag()
            else:
                physics_tag, _ = find_or_create_physics_tag(
                    derived, created_by=created_by or 'rucio_reconcile')

            slug = hashlib.sha1(did.encode()).hexdigest()[:12]
            pcs_name = f'past.{stage}.{version}.{slug}'
            probe = Dataset(
                physics_tag=physics_tag,
                metadata={'source': {'kind': 'rucio_did', 'location': did}},
            )
            probe_detail = physics_config_key(probe)
            key = probe_detail['key']
            if probe_detail['evgen'] is None:
                # An unresolved evgen never matches an edition and never
                # shares a key with another DID — distinct backgrounds
                # must not merge through the unresolved pool.
                key = (key[0], ('unresolved', did), key[2], key[3])
            head = pc_heads.get(key)
            metadata = {
                'stage': stage.lower(),
                'source': {'kind': 'rucio_did', 'location': did},
                'past_output': {
                    'campaign_name': campaign_name,
                    'stage': stage, 'version': version,
                    'rses': rses, 'complete': complete,
                    'path': decomposed, 'filters': filters,
                    'index_path': 'rucio_reconcile',
                },
            }
            if head is not None:
                # The configuration already has an edition: this dataset
                # joins it as a physical sibling; outputs land on the
                # edition's task. Never a duplicate edition.
                sibling, was_created = Dataset.objects.get_or_create(
                    dataset_name=pcs_name, block_num=1,
                    defaults=dict(
                        scope=head.scope, did=f'group.EIC:{pcs_name}.b1',
                        detector_version=head.detector_version,
                        detector_config=head.detector_config,
                        campaign=campaign,
                        composed_name=head.composed_name,
                        physics_tag=head.physics_tag,
                        evgen_tag=head.evgen_tag, simu_tag=head.simu_tag,
                        reco_tag=head.reco_tag,
                        background_tag=head.background_tag,
                        sample_name=head.sample_name,
                        file_count=files, data_size=size,
                        metadata=metadata,
                        created_by=created_by or 'rucio_reconcile',
                    ))
                if not was_created:
                    sibling.file_count = files
                    sibling.data_size = size
                    sibling.metadata = metadata
                    sibling.save(update_fields=['file_count', 'data_size',
                                                'metadata'])
                _upsert_task_output(
                    _identity_task(head.composed_name),
                    _outputs_entry(did, stage, version, filters, rses,
                                   files, size, complete))
                attached += 1
                continue

            # No edition of this configuration exists: create one,
            # past-ingest style (anchor evgen/simu/reco, refinement is
            # curation), with its own past_output task.
            edition, _ = Dataset.objects.get_or_create(
                dataset_name=pcs_name, block_num=1,
                defaults=dict(
                    scope='group.EIC', did=f'group.EIC:{pcs_name}.b1',
                    detector_version=version,
                    detector_config=decomposed.get('detector_config', ''),
                    campaign=campaign,
                    physics_tag=physics_tag, evgen_tag=anchor_evgen,
                    simu_tag=anchor_simu, reco_tag=anchor_reco,
                    file_count=files, data_size=size,
                    description='', metadata=metadata,
                    created_by=created_by or 'rucio_reconcile',
                ))
            entry = _outputs_entry(did, stage, version, filters, rses,
                                   files, size, complete)
            task = ProdTask.objects.filter(name=pcs_name).first()
            if task is None:
                ProdTask.objects.create(
                    name=pcs_name, status='past_output', description='',
                    dataset=edition, prod_config=anchor_cfg,
                    campaign=campaign,
                    overrides={'outputs': [entry]},
                    created_by=created_by or 'rucio_reconcile')
            else:
                _upsert_task_output(task, entry)
            pc_heads[key] = edition
            created += 1

    summary = {'campaign': campaign_name, 'updated': updated,
               'attached': attached, 'created': created,
               'unresolved': len(unresolved)}
    log_epicprod_action(
        'catalog-sync', 'rucio_reconcile',
        subject_type='campaign', subject_key=campaign_name,
        username=created_by,
        sublevel='normal', live_default=True,
        message=(f'{campaign_name} reconciled firsthand from Rucio: '
                 f'{updated} updated, {attached} attached to existing '
                 f'editions, {created} new, {len(unresolved)} unresolved '
                 f'left to curation'),
        **summary,
    )
    if unresolved:
        _log.warning('rucio_reconcile %s unresolved DIDs: %s',
                     campaign_name, unresolved[:10])
    return {**summary, 'unresolved_dids': unresolved}
