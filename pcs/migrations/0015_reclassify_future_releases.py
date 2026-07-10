from django.db import migrations


def _vt(v):
    return tuple(int(p) if p.isdigit() else -1 for p in str(v or '').split('.'))


def reclassify_future_releases(apps, schema_editor):
    """A past-imported release whose version is newer than the current campaign
    is a future release, not past. The importer forced everything to 'past';
    move the newer ones to 'future' (catches e.g. RECO/26.06.0 while current is
    26.05.0). No-op when there is no current campaign."""
    Campaign = apps.get_model('pcs', 'Campaign')
    current = Campaign.objects.filter(lifecycle='current').first()
    if not current:
        return
    cur = _vt(current.name)
    for c in Campaign.objects.filter(lifecycle='past'):
        version = c.name.split('/', 1)[1] if '/' in c.name else c.name
        if _vt(version) > cur:
            c.lifecycle = 'future'
            c.save(update_fields=['lifecycle'])


class Migration(migrations.Migration):

    dependencies = [
        ('pcs', '0014_complete_physics_schema'),
    ]

    operations = [
        migrations.RunPython(reclassify_future_releases, migrations.RunPython.noop),
    ]
