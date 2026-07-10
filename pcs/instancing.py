"""Campaign instancing: the continuum's forward mechanism
(CAMPAIGN_CONTINUUM.md).

Instancing carries a campaign's physics forward: group the source
campaign's editions by physics configuration, apply each configuration's
disposition, and mint the target campaign's editions — merging onto
editions the ingest has already observed, never duplicating.

The plan is computed first and is the review surface: what would be
minted, what merges onto existing editions, what the dispositions retire
or hold, and what is not safe to act on (unresolved identities, editions
of one configuration with conflicting dispositions). Execution consumes
a reviewed plan; nothing here mutates until the operator fires it.
"""
import logging

from django.db import transaction

from .models import Campaign, Dataset, ProdTask
from .physics_config import group_editions, physics_config_key

_log = logging.getLogger(__name__)

# Request context carried from the source edition's task onto the target
# edition's: everything that makes a row a working-catalog row.
_CARRY_FIELDS = ('prod_config', 'request', 'requestor', 'priority',
                 'pre_tdr_use', 'early_science_use', 'other_use',
                 'new_request', 'csv_file', 'description')


def _campaign_heads(campaign_name):
    """One head row per composed identity in the campaign."""
    return list(
        Dataset.objects.filter(campaign__name=campaign_name)
        .select_related('physics_tag', 'evgen_tag', 'background_tag',
                        'campaign')
        .order_by('composed_name', 'block_num', 'pk')
        .distinct('composed_name'))


def plan_campaign_instancing(source_campaign, target_campaign):
    """The instancing plan from source to target campaign.

    Classes, each item carrying its configuration key and edition names:

    - ``merge``  — continuing configuration whose target edition the
      ingest already observed and whose task still awaits adoption: the
      working context attaches to the existing edition.
    - ``aligned`` — continuing configuration whose target edition exists
      with a working task already: instancing has nothing to do.
    - ``mint``   — continuing configuration with no target edition yet:
      a new edition is created.
    - ``hold``   — held in the catalog, no target production.
    - ``final``  — produced its last campaign; ``replaced_by`` carried
      when a successor is designated.
    - ``no_context`` — continuing configuration whose source edition has
      no working task to carry request context from; instancing cannot
      act, curation supplies the context.
    - ``name_collision`` — the edition this configuration would mint is
      named identically to an existing target edition of a *different*
      resolved configuration: the anchor-tag ambiguity (one tag label
      covering several generators). Tag refinement resolves it; minting
      would corrupt identity, so it is excluded and listed.
    - ``unresolved`` — the configuration identity could not be resolved
      (the curation pool); acting on it risks duplication, so it is
      excluded from action and listed.
    - ``conflict`` — editions grouping to one configuration disagree on
      disposition; a human reconciles before instancing touches it.
    - ``target_only`` — target editions with no source counterpart
      (genuinely new in the target, or unresolved on either side);
      informational.

    Pending AI disposition proposals are not consumed — the plan reads
    decided state only, so deciding proposals changes the plan.

    Dispositions are configuration-level decisions stored on whichever
    edition the decider touched (PCS.md) — commonly the current
    campaign's, while the plan may source from a newer producing
    campaign. They are therefore resolved across ALL campaigns'
    editions of each configuration: a deliberate hold or final anywhere
    decides it (the default ``continue`` is non-information); hold and
    final disagreeing across editions is a conflict for a human.
    """
    source_groups = group_editions(_campaign_heads(source_campaign))
    target_groups = group_editions(_campaign_heads(target_campaign))
    # Configuration-wide decided state: the non-default rows are few;
    # resolve each to its configuration key and union into the read.
    decided_states = {}
    decided_replaced = {}
    for row in (Dataset.objects.exclude(propagation='continue')
                .select_related('physics_tag', 'evgen_tag',
                                'background_tag')
                .order_by('composed_name', 'block_num', 'pk')
                .distinct('composed_name')):
        row_key = physics_config_key(row)['key']
        decided_states.setdefault(row_key, set()).add(row.propagation)
        if row.replaced_by:
            decided_replaced.setdefault(row_key, set()).add(row.replaced_by)
    # Adoption state is identity-level: one identity carries one working
    # task (composed-name-as-identity), and a multi-row identity's
    # sibling tasks remain output records. An identity is awaiting
    # adoption only while NO working task exists for it.
    target_working = set(
        ProdTask.objects.filter(campaign__name=target_campaign)
        .exclude(status='past_output')
        .values_list('dataset__composed_name', flat=True))
    awaiting_adoption = set(
        ProdTask.objects.filter(campaign__name=target_campaign,
                                status='past_output')
        .values_list('dataset__composed_name', flat=True)) - target_working
    source_with_context = set(
        ProdTask.objects.filter(campaign__name=source_campaign)
        .exclude(status='past_output')
        .values_list('dataset__composed_name', flat=True))

    target_names = {d.composed_name
                    for g in target_groups.values()
                    for d, _ in g['editions']}

    plan = {'source_campaign': source_campaign,
            'target_campaign': target_campaign,
            'merge': [], 'aligned': [], 'mint': [], 'no_context': [],
            'name_collision': [], 'hold': [], 'final': [],
            'unresolved': [], 'conflict': [], 'target_only': []}

    for key, group in source_groups.items():
        editions = group['editions']
        item = {
            'pc': key,
            'source_editions': [d.composed_name for d, _ in editions],
        }
        if not all(detail['resolved'] for _, detail in editions):
            plan['unresolved'].append(item)
            continue
        decided = ({d.propagation for d, _ in editions}
                   | decided_states.get(key, set())) - {'continue'}
        if len(decided) > 1:
            item['dispositions'] = sorted(decided)
            plan['conflict'].append(item)
            continue
        disposition = decided.pop() if decided else 'continue'
        if disposition == 'hold':
            plan['hold'].append(item)
            continue
        if disposition == 'final':
            replaced = sorted({d.replaced_by for d, _ in editions
                               if d.replaced_by}
                              | decided_replaced.get(key, set()))
            if replaced:
                item['replaced_by'] = replaced
            plan['final'].append(item)
            continue
        has_context = any(name in source_with_context
                          for name in item['source_editions'])
        if key in target_groups:
            names = [d.composed_name
                     for d, _ in target_groups[key]['editions']]
            item['target_editions'] = names
            if not any(name in awaiting_adoption for name in names):
                plan['aligned'].append(item)
            elif has_context:
                plan['merge'].append(item)
            else:
                plan['no_context'].append(item)
        elif has_context:
            expected = _minted_name(editions[0][0], target_campaign)
            if expected in target_names:
                item['collides_with'] = expected
                plan['name_collision'].append(item)
            else:
                plan['mint'].append(item)
        else:
            plan['no_context'].append(item)

    for key, group in target_groups.items():
        if key not in source_groups:
            plan['target_only'].append({
                'pc': key,
                'target_editions': [d.composed_name
                                    for d, _ in group['editions']],
            })

    plan['summary'] = {cls: len(plan[cls]) for cls in
                       ('merge', 'aligned', 'mint', 'no_context',
                        'name_collision', 'hold', 'final', 'unresolved',
                        'conflict', 'target_only')}
    return plan


def _minted_name(source_head, target_version):
    """The name a minted target edition would carry — the builder run
    over the source head's composition with the target version segment
    (transient instance; build_dataset_name stays the single authority)."""
    return Dataset(
        scope=source_head.scope,
        detector_version=target_version,
        detector_config=source_head.detector_config,
        physics_tag=source_head.physics_tag,
        evgen_tag=source_head.evgen_tag,
        simu_tag=source_head.simu_tag,
        reco_tag=source_head.reco_tag,
        background_tag=source_head.background_tag,
        sample_name=source_head.sample_name,
    ).build_dataset_name()


def _source_task(dataset):
    """The source edition's working task (request context carrier)."""
    return (ProdTask.objects.filter(dataset__composed_name=dataset)
            .exclude(status='past_output')
            .select_related('prod_config').order_by('pk').first())


def _source_task_for(item):
    """The first working task across the configuration's source editions
    — the same any-edition rule the plan classifies with."""
    for name in item['source_editions']:
        task = _source_task(name)
        if task is not None:
            return task
    return None


def _carry_context(source_task, target_task):
    """Attach the source task's request context to the target task,
    preserving the target's own outputs record."""
    for field in _CARRY_FIELDS:
        setattr(target_task, field, getattr(source_task, field))
    overrides = dict(target_task.overrides or {})
    csv_block = (source_task.overrides or {}).get('csv_import')
    if csv_block:
        overrides['csv_import'] = csv_block
    target_task.overrides = overrides


def execute_campaign_instancing(source_campaign, target_campaign, *,
                                created_by=''):
    """Execute the instancing plan: populate the target campaign's
    working catalog from the source campaign (CAMPAIGN_CONTINUUM.md).

    The plan is recomputed at execution — a reviewed plan that has gone
    stale is never blindly applied. Per class:

    - **merge**: the ingested target edition's task becomes a working
      task — renamed to its composed name (composed-name-as-identity;
      the legacy name keeps resolving), status ``submitted`` (adopted
      production, already producing), the source task's request context
      attached, its recorded outputs preserved.
    - **mint**: a new target edition — Dataset named by the builder with
      the target version segment (source tags and sample carried, the
      target campaign bound) and a ``draft`` task carrying the request
      context: planned, not yet produced.
    - hold / final / unresolved / conflict / target_only: untouched,
      counted.

    Idempotent by construction: a re-run classifies previously minted
    editions as merges and re-applies context. One
    ``campaign_instancing`` action-stream event per run.
    """
    from monitor_app.epicprod_logging import log_epicprod_action

    plan = plan_campaign_instancing(source_campaign, target_campaign)
    # The batch-derived next campaign may have no row until its first
    # population; created here it enters as lifecycle 'future' (the
    # model default) and the normal rotation takes over from there.
    target, _created = Campaign.objects.get_or_create(
        name=target_campaign,
        defaults={'created_by': created_by or 'instancing'})
    merged, minted, skipped_no_task, errors = [], [], [], []

    with transaction.atomic():
        for item in plan['merge']:
            source_task = _source_task_for(item)
            if source_task is None:
                skipped_no_task.append(item['source_editions'][0])
                continue
            for name in item['target_editions']:
                # One working task per identity: adopt the first output
                # record; a multi-row identity's siblings remain output
                # records, and an identity already carrying a working
                # task is never re-adopted (rename would collide).
                if ProdTask.objects.filter(dataset__composed_name=name)\
                        .exclude(status='past_output').exists():
                    continue
                task = (ProdTask.objects
                        .filter(dataset__composed_name=name,
                                status='past_output')
                        .order_by('pk').first())
                if task is None:
                    skipped_no_task.append(name)
                    continue
                _carry_context(source_task, task)
                task.name = name
                task.status = 'submitted'
                task.save()
                merged.append(name)

        for item in plan['mint']:
            source_head = (Dataset.objects
                           .filter(composed_name=item['source_editions'][0])
                           .order_by('block_num', 'pk')
                           .select_related().first())
            source_task = _source_task_for(item)
            if source_head is None or source_task is None:
                skipped_no_task.append(item['source_editions'][0])
                continue
            edition = Dataset(
                scope=source_head.scope,
                detector_version=target_campaign,
                detector_config=source_head.detector_config,
                campaign=target,
                physics_tag=source_head.physics_tag,
                evgen_tag=source_head.evgen_tag,
                simu_tag=source_head.simu_tag,
                reco_tag=source_head.reco_tag,
                background_tag=source_head.background_tag,
                sample_name=source_head.sample_name,
                propagation='continue',
                blocks=1, block_num=1,
                metadata={'source': {'kind': 'campaign_instancing',
                                     'location': source_head.composed_name}},
                created_by=created_by or 'campaign_instancing',
            )
            name = edition.build_dataset_name()
            edition.dataset_name = name
            edition.composed_name = name
            edition.did = f'{edition.scope}:{name}.b1'
            try:
                edition.save()
            except Exception as e:                            # noqa: BLE001
                errors.append(f'{name}: {e}')
                continue
            task = ProdTask(
                name=name, status='draft', dataset=edition, campaign=target,
                overrides={}, created_by=created_by or 'campaign_instancing',
            )
            _carry_context(source_task, task)
            task.save()
            minted.append(name)

    summary = {**plan['summary'], 'merged_tasks': len(merged),
               'minted_editions': len(minted),
               'skipped_no_task': len(skipped_no_task),
               'errors': len(errors)}
    log_epicprod_action(
        'web', 'campaign_instancing',
        subject_type='campaign', subject_key=target_campaign,
        username=created_by,
        sublevel='high', live_default=True,
        outcome='ok' if not errors else 'error',
        reason='; '.join(errors[:5]),
        message=(f'campaign instancing {source_campaign} -> '
                 f'{target_campaign}: {len(minted)} edition(s) minted, '
                 f'{len(merged)} task(s) adopted; '
                 f"{plan['summary']['hold']} held, "
                 f"{plan['summary']['final']} final, "
                 f"{plan['summary']['unresolved']} unresolved left to "
                 f'curation'),
        **summary,
    )
    return {'merged': merged, 'minted': minted,
            'skipped_no_task': skipped_no_task, 'errors': errors,
            'summary': summary}
