"""Stored EVGEN door paths move from EIC-XRD's former prefix,
/volatile/eic/EPIC, to its present one, /cache/eic/EPIC: the source location
of a dataset, the legacy ``csv_file`` of a task, and the declared input path
of a request. The readers take the present prefix only
(swf_epicprod.door_paths). The raw catalogue row a dataset keeps in
``metadata['csv_import']`` is a record of what was imported and is left as
it was written."""
from django.db import migrations

OLD = '/volatile/eic/EPIC/'
NEW = '/cache/eic/EPIC/'


def _moved(path):
    return NEW + path[len(OLD):]


def to_cache(apps, schema_editor):
    Dataset = apps.get_model('pcs', 'Dataset')
    ProdTask = apps.get_model('pcs', 'ProdTask')
    ProdRequest = apps.get_model('pcs', 'ProdRequest')
    counts = {'datasets': 0, 'tasks': 0, 'requests': 0}
    for ds in Dataset.objects.filter(metadata__source__location__startswith=OLD).iterator():
        ds.metadata['source']['location'] = _moved(ds.metadata['source']['location'])
        ds.save(update_fields=['metadata'])
        counts['datasets'] += 1
    for task in ProdTask.objects.filter(csv_file__startswith=OLD).iterator():
        task.csv_file = _moved(task.csv_file)
        task.save(update_fields=['csv_file'])
        counts['tasks'] += 1
    for req in ProdRequest.objects.filter(simu_path__startswith=OLD).iterator():
        req.simu_path = _moved(req.simu_path)
        req.save(update_fields=['simu_path'])
        counts['requests'] += 1
    print(f"\n  source paths moved to {NEW}: {counts['datasets']} datasets, "
          f"{counts['tasks']} tasks, {counts['requests']} requests", end='')


class Migration(migrations.Migration):

    dependencies = [
        ('pcs', '0010_identity_guards'),
    ]

    operations = [migrations.RunPython(to_cache, migrations.RunPython.noop)]
