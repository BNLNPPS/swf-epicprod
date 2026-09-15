"""Registered samples nobody asked for: from a registered EVGEN dataset to
a physics configuration, request and draft task in one act
(docs/EPICPROD_EVGEN_INPUTS.md § From a registered sample to a task).

``derive_registered_sample`` reads a dataset's physics from its path with
the ingest's derivation; ``registered_sample_precondition`` says whether
the record already holds the sample; ``registered_sample_intake`` is the
executor an approved ``registered_sample`` proposal runs, and the call a
person makes by hand: the EVGEN-stage record with the dataset's tail as
its source location, the production edition on the campaign's release
pair, the request with the requestor, target and priority, and the draft
task, in one transaction with one origin-stamped event.
"""
from django.db import transaction

from .physics_match import (derive_background, derive_evgen, derive_physics,
                            single_particle_angle)
from .physics_config import config_name


def did_tail(did):
    """``DIS/...`` from ``epic:/EVGEN/DIS/...``, ``/EVGEN/DIS/...`` or the
    bare tail."""
    name = str(did or '').strip()
    if ':' in name.split('/', 1)[0]:
        name = name.split(':', 1)[1]
    name = name.strip('/')
    if name.startswith('EVGEN/'):
        name = name[len('EVGEN/'):]
    return name


def derive_registered_sample(did):
    """The identity a registered dataset's path states: physics, evgen,
    background and sample parameters as the ingest derives them, plus the
    physics tag and configuration the record already holds for them.
    Returns (identity, reason); identity None when the path does not
    derive, with the reason."""
    from .ingest import _beam_from, _evgen_from_catalog
    from .models import PhysicsConfig
    from .services import (ServiceError, _no_signal_physics_tag,
                           find_or_create_background_tag, find_or_create_physics_tag)
    tail = did_tail(did)
    if not tail:
        return None, 'no EVGEN path'
    path = f'EVGEN/{tail}'
    derived = derive_physics(path, beam=_beam_from({}, path))
    if derived is None:
        return None, f'no physics area recognized in {path}'
    is_background = derived.get('process') in ('BEAMGAS', 'SYNRAD')
    background = None
    background_label = ''
    if is_background:
        physics_tag = _no_signal_physics_tag()
        background = derive_background(path)
        if background:
            bg_tag, _ = find_or_create_background_tag(background, dry_run=True)
            background_label = bg_tag.tag_label if bg_tag else ''
        physics_new = False
    else:
        try:
            physics_tag, _ = find_or_create_physics_tag(derived, dry_run=True)
        except ServiceError as e:
            return None, f'physics cannot be tagged: {e.detail}'
        physics_new = physics_tag is None
    evgen = derive_evgen(path)
    if evgen is None:
        identity, _n, trusted = _evgen_from_catalog(path)
        if identity is not None and trusted:
            evgen = {'generator': identity[0], 'generator_version': identity[1]}
            if identity[2]:
                evgen['radiative'] = identity[2]
            if len(identity) > 3 and identity[3]:
                evgen['afterburner_preset'] = identity[3]
    if evgen is None:
        return None, 'generator and version not resolved from the path'
    sample = single_particle_angle(path)
    evgen_tuple = (str(evgen.get('generator', '')).lower(),
                   str(evgen.get('generator_version', '')).lower(),
                   str(evgen.get('radiative', '')).lower(),
                   str(evgen.get('afterburner_preset', '')).lower())
    key = ''
    pc = None
    if not physics_new:
        key = config_name({'key': (physics_tag.tag_label, evgen_tuple,
                                   background_label, sample),
                           'evgen': evgen_tuple})
        pc = PhysicsConfig.objects.filter(config_key=key).first()
    return {
        'did': f'epic:/EVGEN/{tail}', 'tail': tail, 'path': path,
        'physics': derived, 'evgen': evgen, 'background': background,
        'sample': sample, 'is_background': is_background,
        'physics_tag': physics_tag.tag_label if physics_tag else '',
        'physics_new': physics_new, 'config_key': key,
        'pc': pc.label if pc is not None else '',
    }, ''


def registered_sample_precondition(did):
    """What must still be so for an intake to proceed: no PCS evgen
    dataset resolves to the DID and no request anchors on the sample's
    configuration. Returns {'matched_by': [...], 'requested_by': [...]};
    both empty means the record does not hold the sample."""
    from .models import Dataset, ProdTask
    tail = did_tail(did)
    full = f'epic:/EVGEN/{tail}'
    matched = []
    for ds in Dataset.objects.filter(metadata__contains={'stage': 'evgen'}):
        for m in ((ds.metadata or {}).get('rucio') or {}).get('matched') or []:
            if str(m.get('did') or '') == full:
                matched.append(ds.composed_name)
                break
    requested, tasks = [], []
    identity, _ = derive_registered_sample(did)
    if identity and identity.get('pc'):
        from .services import pc_request_projection
        editions = list(Dataset.objects.filter(physics_config__label=identity['pc']))
        for reqs in pc_request_projection(editions).values():
            requested.extend(r.pk for r in reqs)
        # A configuration with a task has been asked for, whatever route
        # the ask took (a legacy task assimilated from PanDA has no request).
        tasks = list(ProdTask.objects.filter(dataset__in=editions)
                     .order_by('pk').values_list('name', flat=True)[:5])
    return {'matched_by': matched, 'requested_by': sorted(set(requested)),
            'tasks': tasks}


def _origin_attrs(origin):
    """The origin stamp of the executed event, as every executor writes it:
    the approving proposal's identity, or manual."""
    origin = origin or {}
    if origin.get('kind') == 'ai_proposal':
        return {k: v for k, v in {
            'origin': 'ai_proposal', 'proposer': origin.get('proposer', ''),
            'proposal_ref': origin.get('ref', ''),
            'batch_id': origin.get('batch_id', ''),
            'proposed_at': origin.get('proposed_at', ''),
        }.items() if v}
    return {'origin': 'manual'}


def registered_sample_intake(did, campaign_name, *, requestor, nevents=None,
                             priority=None, changed_by='', origin=None, comment=''):
    """Take a registered EVGEN dataset into the record as a physics
    configuration of ``campaign_name``: the EVGEN-stage record (its source
    location the dataset's tail, so the next assimilation matches it),
    the production edition on the campaign's release pair, the request
    (requestor, event target, priority) anchored on the production
    edition, the draft task on it. One transaction, one origin-stamped
    ``registered_sample_intake`` event. Returns {'input_edition',
    'edition', 'request', 'task', 'log_id'}. Refuses, with the reason,
    a path that does not derive, a sample the record already holds, an
    unknown campaign or a missing requestor."""
    from monitor_app.epicprod_logging import log_epicprod_action
    from .ingest import _production_edition
    from .models import Campaign, Dataset, ProdConfig, ProdRequest, ProdTask
    from .services import (PLACEHOLDER_PRODCONFIG_NAME, ServiceError,
                           _ensure_csvimport_anchors, _ensure_r0_stage_tag,
                           _ensure_s0_stage_tag, _no_signal_physics_tag,
                           find_or_create_background_tag, find_or_create_evgen_tag,
                           find_or_create_physics_tag, prodtask_apply_request)

    requestor = str(requestor or '').strip()
    if not requestor:
        raise ServiceError('a requestor is required')
    identity, reason = derive_registered_sample(did)
    if identity is None:
        raise ServiceError(reason)
    held = registered_sample_precondition(did)
    if held['matched_by'] or held['requested_by'] or held['tasks']:
        raise ServiceError(
            f"{identity['did']} is already in the record: "
            + (f"matched by {', '.join(held['matched_by'])}" if held['matched_by'] else '')
            + (' and ' if held['matched_by'] and held['requested_by'] else '')
            + (f"requested by {', '.join(str(r) for r in held['requested_by'])}"
               if held['requested_by'] else '')
            + (f"; tasks {', '.join(held['tasks'])}" if held['tasks'] else ''))
    campaign = Campaign.objects.filter(name=str(campaign_name or '').strip()).first()
    if campaign is None:
        raise ServiceError(f'campaign {campaign_name!r} is not defined')
    det_version = str((campaign.data or {}).get('detector_version') or '').strip()
    det_config = str((campaign.data or {}).get('detector_config') or 'epic_craterlake').strip()
    if not det_version:
        # The edition the campaign's other editions carry names the version.
        sibling = (Dataset.objects.filter(campaign=campaign)
                   .exclude(detector_version='').order_by('-pk').first())
        if sibling is None:
            raise ServiceError(f'campaign {campaign.name} names no detector version')
        det_version, det_config = sibling.detector_version, sibling.detector_config
    if nevents is not None:
        try:
            nevents = int(nevents)
        except (TypeError, ValueError):
            raise ServiceError(f'nevents must be an integer; got {nevents!r}')
        if nevents <= 0:
            raise ServiceError('nevents must be positive')
    if priority not in (None, 1, 2, 3):
        raise ServiceError('priority must be 1, 2, 3 or absent')
    created_by = changed_by or 'registered_sample'

    with transaction.atomic():
        if identity['is_background']:
            physics_tag = _no_signal_physics_tag()
            background_tag = None
            if identity['background']:
                background_tag, _ = find_or_create_background_tag(
                    identity['background'], created_by=created_by)
        else:
            physics_tag, _ = find_or_create_physics_tag(identity['physics'], created_by=created_by)
            background_tag = None
        evgen_tag, _ = find_or_create_evgen_tag(identity['evgen'], created_by=created_by)
        s0 = _ensure_s0_stage_tag(created_by=created_by)
        r0 = _ensure_r0_stage_tag(created_by=created_by)
        description = f"registered EVGEN sample {identity['did']}"
        probe = Dataset(scope='group.EIC', detector_version=det_version,
                        detector_config=det_config, physics_tag=physics_tag,
                        evgen_tag=evgen_tag, simu_tag=s0, reco_tag=r0,
                        background_tag=background_tag, sample_name=identity['sample'])
        composed = probe.build_dataset_name()
        input_edition = Dataset.objects.filter(composed_name=composed).first()
        if input_edition is None:
            input_edition = Dataset(
                scope='group.EIC', detector_version=det_version,
                detector_config=det_config, campaign=campaign,
                physics_tag=physics_tag, evgen_tag=evgen_tag,
                simu_tag=s0, reco_tag=r0, background_tag=background_tag,
                sample_name=identity['sample'], description=description,
                metadata={'stage': 'evgen',
                          'source': {'kind': 'rucio', 'location': identity['path'],
                                     'did': identity['did']}},
                created_by=created_by)
            try:
                input_edition.save()
            except Exception as e:                              # noqa: BLE001
                raise ServiceError(f'{composed}: {e}')
        edition = _production_edition(input_edition, created_by=created_by)
        if nevents is not None and edition.expected_events is None:
            edition.expected_events = nevents
            edition.expected_events_source = 'requested'
            edition.save(update_fields=['expected_events', 'expected_events_source'])
        source_row = f"rucio:{identity['did']}"
        req = ProdRequest.objects.filter(source_row=source_row).first()
        if req is None:
            req = ProdRequest.objects.create(
                requestor=requestor, simu_path=identity['path'],
                gen_config=' '.join(p for p in (
                    identity['evgen'].get('generator'),
                    identity['evgen'].get('generator_version')) if p),
                nevents=nevents, priority=priority,
                background=(background_tag.tag_label if background_tag else ''),
                description=description, new_request=True, status='new',
                source_row=source_row,
                data={'physics_config_anchor': edition.composed_name,
                      'registered_sample': {'did': identity['did'],
                                            'created_by': created_by}},
                created_by=created_by)
        task_name = identity['path']
        task = ProdTask.objects.filter(name=task_name).first()
        if task is None:
            cfg = ProdConfig.objects.filter(name=PLACEHOLDER_PRODCONFIG_NAME).first()
            if cfg is None:
                cfg = _ensure_csvimport_anchors()[4]
            try:
                task = ProdTask.objects.create(
                    name=task_name, description=description, status='draft',
                    dataset=edition, prod_config=cfg, campaign=campaign,
                    overrides={'registered_sample': {'did': identity['did']}},
                    created_by=created_by)
            except Exception as e:                              # noqa: BLE001
                raise ServiceError(f'{task_name}: {e}')
            prodtask_apply_request(task, req)
        log_id = log_epicprod_action(
            'web', 'registered_sample_intake', subject_type='dataset',
            subject_key=edition.composed_name, username=changed_by,
            sublevel='normal', live_default=True,
            message=(f"registered sample {identity['did']} taken into {campaign.name} "
                     f"as {edition.composed_name}: request {req.pk} ({requestor}), "
                     f"task {task.name}"),
            did=identity['did'], input_edition=input_edition.composed_name,
            edition=edition.composed_name, request=req.pk, task=task.name,
            requestor=requestor, nevents=nevents, priority=priority,
            comment=comment or '', **_origin_attrs(origin))
    return {'input_edition': input_edition.composed_name,
            'edition': edition.composed_name, 'request': req.pk,
            'task': task.name, 'log_id': log_id}
