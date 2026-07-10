"""Campaign identity cleanup: one bare-named campaign row per version.

Merges the stage-prefixed campaign rows ('FULL/<v>', 'RECO/<v>') created by
the epic-prod past-campaign ingest into the bare-named campaign for the same
version (created lifecycle='past' when absent), repointing dataset and task
rows and folding the per-stage totals into stage-keyed
``data['past_summary'][stage]``. Also folds any legacy
``overrides['past_output']`` task block onto the unified
``overrides['outputs']`` schema (EPICPROD_DATA_LINEAGE.md), and closes out
26.06.0 as a past interim campaign.

Data migration, forward-only; the reverse is a no-op.
"""

from django.db import migrations


def _fold_summary(bare_data, stage, old_summary):
    data = dict(bare_data or {})
    past_summary = data.get('past_summary')
    if not isinstance(past_summary, dict) or 'file_count' in past_summary:
        # Flat legacy shape on the bare row (single stage): re-key it.
        if isinstance(past_summary, dict) and past_summary.get('stage'):
            past_summary = {past_summary['stage']: {
                'file_count': past_summary.get('file_count', 0),
                'data_size_bytes': past_summary.get('data_size_bytes', 0),
            }}
        else:
            past_summary = {}
    past_summary[stage] = {
        'file_count': (old_summary or {}).get('file_count', 0),
        'data_size_bytes': (old_summary or {}).get('data_size_bytes', 0),
    }
    data['past_summary'] = past_summary
    return data


def merge_stage_campaigns(apps, schema_editor):
    Campaign = apps.get_model('pcs', 'Campaign')
    Dataset = apps.get_model('pcs', 'Dataset')
    ProdTask = apps.get_model('pcs', 'ProdTask')

    for camp in list(Campaign.objects.filter(name__contains='/')):
        stage, _, version = camp.name.partition('/')
        bare = Campaign.objects.filter(name=version).first()
        if bare is None:
            bare = Campaign.objects.create(
                name=version,
                lifecycle='past',
                description=f'Campaign {version} (epic-prod ingest)',
                created_by='campaign_merge_migration',
            )
        Dataset.objects.filter(campaign=camp).update(campaign=bare)
        ProdTask.objects.filter(campaign=camp).update(campaign=bare)
        bare.data = _fold_summary(
            bare.data, stage, (camp.data or {}).get('past_summary'))
        bare.save(update_fields=['data'])
        camp.delete()


def fold_legacy_past_output(apps, schema_editor):
    ProdTask = apps.get_model('pcs', 'ProdTask')
    qs = (ProdTask.objects
          .filter(overrides__has_key='past_output')
          .select_related('dataset'))
    for task in qs:
        overrides = dict(task.overrides or {})
        legacy = overrides.pop('past_output', None)
        if legacy and not overrides.get('outputs'):
            metadata = (task.dataset.metadata or {}) if task.dataset_id else {}
            did = ((metadata.get('source') or {}).get('location') or '')
            rses = [{'rse': r.get('name'), 'files': r.get('files'),
                     'total': r.get('total'),
                     'complete': r.get('status') == 'complete'}
                    for r in (legacy.get('rses') or [])]
            overrides['outputs'] = [{
                'did': did,
                'stage': legacy.get('stage', ''),
                'version': legacy.get('version', ''),
                'filters': legacy.get('filters', {}),
                'rses': rses,
                'file_count': task.dataset.file_count if task.dataset_id else 0,
                'bytes': task.dataset.data_size if task.dataset_id else 0,
                'complete': legacy.get('complete', True),
            }]
        task.overrides = overrides
        task.save(update_fields=['overrides', 'updated_at'])


def close_out_26_06(apps, schema_editor):
    Campaign = apps.get_model('pcs', 'Campaign')
    Campaign.objects.filter(name='26.06.0').update(lifecycle='past')


class Migration(migrations.Migration):

    dependencies = [
        ('pcs', '0023_dataset_propagation'),
    ]

    operations = [
        migrations.RunPython(merge_stage_campaigns, migrations.RunPython.noop),
        migrations.RunPython(fold_legacy_past_output, migrations.RunPython.noop),
        migrations.RunPython(close_out_26_06, migrations.RunPython.noop),
    ]
