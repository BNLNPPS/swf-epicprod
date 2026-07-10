from django.db import migrations


def restore_detector_version_composed_name(apps, schema_editor):
    """Undo campaign-keyed composed names.

    Dataset identity is tied to detector_version/detector_config plus tags. The
    campaign FK remains as bookkeeping only.
    """
    Dataset = apps.get_model('pcs', 'Dataset')
    rows = list(Dataset.objects.select_related(
        'physics_tag', 'evgen_tag', 'simu_tag', 'reco_tag', 'background_tag'
    ))
    for ds in rows:
        name = (f"{ds.scope}.{ds.detector_version}.{ds.detector_config}"
                f".{ds.physics_tag.tag_label}.{ds.evgen_tag.tag_label}"
                f".{ds.simu_tag.tag_label}.{ds.reco_tag.tag_label}")
        if ds.background_tag_id:
            name = f"{name}.{ds.background_tag.tag_label}"
        if ds.sample_name:
            name = f"{name}.{ds.sample_name}"
        ds.composed_name = name
    if rows:
        Dataset.objects.bulk_update(rows, ['composed_name'], batch_size=500)


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('pcs', '0018_dataset_campaign'),
    ]

    operations = [
        migrations.RunPython(restore_detector_version_composed_name, noop_reverse),
    ]
