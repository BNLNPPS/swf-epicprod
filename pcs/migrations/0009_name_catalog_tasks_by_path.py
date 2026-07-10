from django.db import migrations

VOLATILE_PREFIX = '/volatile/eic/EPIC/'


def name_tasks_by_path(apps, schema_editor):
    """Rename imported catalog tasks from the opaque ``csv_import.<hash>`` to
    their human-readable source path — the path already shown as the task title.

    The path (``dataset.metadata.source.location``) is unique per catalog row,
    so it is a valid unique task name; the common ``/volatile/eic/EPIC/`` prefix
    is stripped to match the title. A row with no path keeps its slug name. The
    Rucio DID / dataset name is unchanged — a separate concern. This matches the
    naming the import now produces, so a re-import matches these rows rather than
    duplicating them.
    """
    ProdTask = apps.get_model('pcs', 'ProdTask')
    for t in ProdTask.objects.filter(name__startswith='csv_import.').select_related('dataset'):
        loc = (((t.dataset.metadata or {}).get('source') or {}).get('location') or '').strip()
        if not loc:
            continue
        new = loc[len(VOLATILE_PREFIX):] if loc.startswith(VOLATILE_PREFIX) else loc
        if not new or new == t.name:
            continue
        # Paths are unique in practice; never clobber an existing name if not.
        if ProdTask.objects.filter(name=new).exclude(pk=t.pk).exists():
            continue
        t.name = new
        t.save(update_fields=['name'])


class Migration(migrations.Migration):

    dependencies = [
        ('pcs', '0008_csv_import_into_draft'),
    ]

    operations = [
        migrations.RunPython(name_tasks_by_path, migrations.RunPython.noop),
    ]
