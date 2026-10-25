"""Compose a small whole production sample, with its own identity and target.

This uses the existing sample discriminator. It does not allocate part of a
parent attempt or change the parent's manifest, outputs or completion.
"""
import re
from django.db import transaction

from .models import Dataset, ProdTask


@transaction.atomic
def compose(source, *, sample, event_service, site, created_by):
    from . import services
    from monitor_app.epicprod_logging import log_epicprod_action
    from swf_epicprod.production_es import definition, gun_seed
    from .commands import is_particle_gun, internal_evgen_sample, prodtask_events_per_job
    es = definition(event_service)
    original = source.dataset
    if not is_particle_gun(source):
        raise ValueError('additional production statistics require an internally generated particle gun')
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,19}', sample):
        raise ValueError('sample discriminator must be a short identifier')
    source_path, source_stem = internal_evgen_sample(source)
    if original.background_tag_id:
        raise ValueError('background-mixed samples require a separate ES validation')
    metadata = {
        'production_sample': {'source': source.composed_name, 'events': es['events'],
                              'sample': sample, 'gun_sample': original.sample_name,
                              'seed_scheme': 'stem-sha256-28-and-absolute-range-start',
                              'generation_plan': es['input_dataset'], 'validation': 'pending'},
    }
    ds = Dataset(
        scope=original.scope, detector_version=original.detector_version,
        detector_config=original.detector_config, campaign=source.campaign,
        physics_tag=original.physics_tag, evgen_tag=original.evgen_tag,
        simu_tag=original.simu_tag, reco_tag=original.reco_tag,
        sample_name='.'.join(x for x in (original.sample_name, sample) if x),
        description=f'{es["events"]:,}-event production sample derived from {source.composed_name}',
        metadata=metadata, created_by=created_by, propagation='final')
    name = ds.build_dataset_name()
    if Dataset.objects.filter(composed_name=name).exists():
        raise ValueError('production sample already exists: ' + name)
    ds.dataset_name = name
    ds.did = f'{ds.scope}:{name}.b1'
    ds.save()
    services.dataset_expected_events_set([
        {'name': name, 'expected_events': es['events'], 'source': 'included'}],
        'Standalone whole production sample authorized by Torre Wenaus',
        changed_by=created_by)
    task = ProdTask.objects.create(
        name=name, dataset=ds, prod_config=source.prod_config, campaign=source.campaign,
        request=source.request, requestor=source.requestor, priority=source.priority or 1,
        description=ds.description, created_by=created_by,
        overrides={'event_service': es, 'production_sample_prefix': sample,
                   'copy_reco': True, 'copy_full': False, 'copy_log': False,
                   'use_rucio': True, 'panda_site': site})
    path, stem = internal_evgen_sample(task)
    if es['row'] != f'{path}/{stem},steer,{es["events"]},0000':
        raise ValueError('generation plan row does not match the composed particle-gun sample')
    source_chunks = -(-int(original.expected_events) // prodtask_events_per_job(source))
    old_seeds = {gun_seed(source_stem, i) for i in range(source_chunks)}
    # Check every possible recovered range start, not just normal unit boundaries.
    new_seeds = {gun_seed(stem, i) for i in range(es['events'])}
    if old_seeds & new_seeds or len(new_seeds) != es['events']:
        raise ValueError('sample random streams collide with existing production; choose another sample identifier')
    metadata['production_sample']['seed_overlap_with_source'] = 0
    ds.metadata = metadata
    ds.save(update_fields=['metadata'])
    services.prodtask_place(task, site, changed_by=created_by)
    log_epicprod_action(
        'pcs', 'production_sample_composed', username=created_by,
        subject_type='campaign_task', subject_key=name,
        source_task=source.composed_name, events=es['events'], site=site)
    return task
