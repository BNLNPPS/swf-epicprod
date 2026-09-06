"""Trials: proving a configuration by running it small and for real.

A trial is a standing production task of its own, modelled on the
configuration it proves — the same tags, the same input, the same
payload, the same submission path — differing only in scale, in where
its outputs land, and in that it counts toward no physics
(docs/PCS.md, Trials). It is offered to the requesting physics group,
and what its role is for them is theirs to say.

Its identity is the configuration's name with the logical trial suffix,
``…r1.trial`` and then ``.trial2`` for the variants a configuration
needs before it is right. The trial number lives in the dataset's
metadata and the composed name is derived from it on every save, so the
name and the flag cannot disagree. Nothing here touches the
configuration it proves.
"""
import logging

from django.db import transaction

from .models import Dataset, ProdTask
from .name_tokens import trial_number_from_name

_log = logging.getLogger(__name__)

# What a trial runs by default: enough events to exercise the whole
# chain and see concurrency behave, few enough to finish in minutes.
DEFAULT_TRIAL_EVENTS = 100

# Trial outputs land here, beside the canary's own root, with the
# production substructure beneath so a physics group reads them in the
# shape real data has and nothing can mistake them for production.
TRIAL_OUTPUT_ROOT = 'TEST/trial'

# Long enough for a group to look, short enough that nobody curates it:
# what survives a trial is the acceptance and the record, not the data.
TRIAL_LIFETIME_DAYS = 14


def trials_of(dataset):
    """Every trial dataset of one configuration, oldest number first."""
    if dataset is None:
        return Dataset.objects.none()
    base = dataset.composed_name
    return (Dataset.objects
            .filter(composed_name__startswith=f'{base}.trial')
            .order_by('id'))


def next_trial_number(dataset):
    """The number the next trial of this configuration takes."""
    used = {trial_number_from_name(d.composed_name) for d in trials_of(dataset)}
    number = 1
    while number in used:
        number += 1
    return number


@transaction.atomic
def compose_trial(source_task, events=DEFAULT_TRIAL_EVENTS, site='',
                  created_by='', trial_number=None, prod_config=None,
                  input_did=''):
    """Mint a trial of ``source_task``'s configuration and return its task.

    The trial is a new Dataset and ProdTask carrying the source's tags
    unchanged; only the metadata differs, and the name follows from it.
    The source task and its dataset are not touched — a trial is never
    the production task wearing trial apparatus.
    """
    source = source_task.dataset
    if source is None:
        raise ValueError(
            f'{source_task.name} has no dataset to model a trial on')
    number = int(trial_number or next_trial_number(source))
    events = int(events or DEFAULT_TRIAL_EVENTS)

    # A task's inputs are read from its dataset's matched Rucio entries
    # (ProdTask.inputs, EPICPROD_EVGEN_INPUTS.md), so a trial takes the
    # source's matched inputs, or the one named here when the source has
    # none — which is the case for a configuration PCS adopted from PanDA
    # rather than composed. The assimilation refreshes the detail later.
    matched = list((((source.metadata or {}).get('rucio') or {})
                    .get('matched') or []))
    if not matched and input_did:
        matched = [{'did': input_did, 'stage': 'evgen'}]

    edition = Dataset(
        scope=source.scope,
        detector_version=source.detector_version,
        detector_config=source.detector_config,
        campaign=source.campaign,
        physics_tag=source.physics_tag,
        evgen_tag=source.evgen_tag,
        simu_tag=source.simu_tag,
        reco_tag=source.reco_tag,
        background_tag=source.background_tag,
        sample_name=source.sample_name,
        blocks=1, block_num=1,
        metadata={
            'trial': number,
            'trial_events': events,
            'trial_site': site or '',
            'trial_output_root': TRIAL_OUTPUT_ROOT,
            'trial_lifetime_days': TRIAL_LIFETIME_DAYS,
            'source': {'kind': 'trial', 'location': source.composed_name},
            **({'rucio': {'matched': matched}} if matched else {}),
        },
        created_by=created_by or 'trial',
    )
    # build_dataset_name reads the trial number from metadata, so the
    # composed name carries the suffix without anyone appending it.
    name = edition.build_dataset_name()
    edition.dataset_name = name
    edition.composed_name = name
    edition.did = f'{edition.scope}:{name}.b1'
    edition.save()

    # inputs and input_source_location are derived properties, not
    # fields: a task's input is its dataset's matched entries, set above.
    task = ProdTask(
        name=name, status='draft', dataset=edition,
        campaign=source_task.campaign,
        prod_config=prod_config or source_task.prod_config,
        request=source_task.request,
        requestor=source_task.requestor,
        overrides=dict(source_task.overrides or {}),
        created_by=created_by or 'trial',
    )
    task.save()
    _log.info('trial %s composed from %s (%s events)',
              name, source_task.name, events)
    return task
