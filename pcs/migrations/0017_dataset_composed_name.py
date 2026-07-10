"""Store the composed (tag-based) dataset identity in its own indexed column.

Previously ``Dataset.composed_name`` was a property that rebuilt the name from
the tag FKs on every access, forcing every list/detail view to prefetch five
tag joins just to render or link a task. The name is now a stored column, kept
current on every ``Dataset.save()`` and read directly. This backfill replicates
``build_dataset_name`` for the existing rows (historical models carry no custom
methods); ``tag_label`` is a stored field on each tag model, so the composition
is reproduced exactly.
"""
from django.db import migrations, models


def backfill_composed_name(apps, schema_editor):
    Dataset = apps.get_model('pcs', 'Dataset')
    rows = list(
        Dataset.objects.select_related(
            'physics_tag', 'evgen_tag', 'simu_tag', 'reco_tag', 'background_tag'
        )
    )
    for ds in rows:
        name = (
            f"{ds.scope}.{ds.detector_version}.{ds.detector_config}"
            f".{ds.physics_tag.tag_label}.{ds.evgen_tag.tag_label}"
            f".{ds.simu_tag.tag_label}.{ds.reco_tag.tag_label}"
        )
        if ds.background_tag_id:
            name = f"{name}.{ds.background_tag.tag_label}"
        if ds.sample_name:
            name = f"{name}.{ds.sample_name}"
        ds.composed_name = name
    if rows:
        Dataset.objects.bulk_update(rows, ['composed_name'], batch_size=500)


class Migration(migrations.Migration):

    dependencies = [
        ('pcs', '0016_dataset_sample_name'),
    ]

    operations = [
        migrations.AddField(
            model_name='dataset',
            name='composed_name',
            field=models.CharField(blank=True, db_index=True, default='', max_length=255),
        ),
        migrations.RunPython(backfill_composed_name, migrations.RunPython.noop),
    ]
