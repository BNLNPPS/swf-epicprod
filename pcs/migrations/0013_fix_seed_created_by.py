from django.db import migrations


def fix_seed_created_by(apps, schema_editor):
    """The Background category (digit 6) and its no-signal physics tag (p6001)
    were seeded with created_by='seed' by migration 0010; every other category
    and tag is owned by 'wenaus'. Normalise the two so ownership reads
    consistently across the catalog."""
    for model_name in ('PhysicsCategory', 'PhysicsTag'):
        apps.get_model('pcs', model_name).objects.filter(
            created_by='seed'
        ).update(created_by='wenaus')


class Migration(migrations.Migration):

    dependencies = [
        ('pcs', '0012_unlock_all_tags_draft'),
    ]

    operations = [
        migrations.RunPython(fix_seed_created_by, migrations.RunPython.noop),
    ]
