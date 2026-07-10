import django.db.models.deletion
from django.db import migrations, models


def backfill_campaign(apps, schema_editor):
    """Link each Dataset to its producing campaign and recompute the composed
    name from the campaign name. The campaign is taken from the dataset's
    producing task; failing that, from a Campaign whose name equals the legacy
    detector_version. The composed name is rebuilt from the same formula as
    Dataset.build_dataset_name (campaign name, falling back to detector_version),
    so for current data — where the two are equal — the stored name is
    unchanged."""
    Dataset = apps.get_model('pcs', 'Dataset')
    Campaign = apps.get_model('pcs', 'Campaign')
    by_name = {c.name: c for c in Campaign.objects.all()}
    rows = list(Dataset.objects.select_related(
        'physics_tag', 'evgen_tag', 'simu_tag', 'reco_tag', 'background_tag'
    ).prefetch_related('prod_tasks__campaign'))
    for ds in rows:
        camp = None
        for t in ds.prod_tasks.all():
            if t.campaign_id:
                camp = t.campaign
                break
        if camp is None:
            camp = by_name.get(ds.detector_version)
        ds.campaign = camp
        version = camp.name if camp else ds.detector_version
        name = (f"{ds.scope}.{version}.{ds.detector_config}"
                f".{ds.physics_tag.tag_label}.{ds.evgen_tag.tag_label}"
                f".{ds.simu_tag.tag_label}.{ds.reco_tag.tag_label}")
        if ds.background_tag_id:
            name = f"{name}.{ds.background_tag.tag_label}"
        if ds.sample_name:
            name = f"{name}.{ds.sample_name}"
        ds.composed_name = name
    if rows:
        Dataset.objects.bulk_update(
            rows, ['campaign', 'composed_name'], batch_size=500)


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('pcs', '0017_dataset_composed_name'),
    ]

    operations = [
        migrations.AddField(
            model_name='dataset',
            name='campaign',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='datasets', to='pcs.campaign',
            ),
        ),
        migrations.RunPython(backfill_campaign, noop_reverse),
    ]
