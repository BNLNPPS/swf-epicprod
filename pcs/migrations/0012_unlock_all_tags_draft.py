from django.db import migrations


def unlock_all_tags(apps, schema_editor):
    """Alpha: drop every PCS tag to draft so ops can edit freely while the
    campaign mapping is still being shaped. Reproducibility locking moves to
    submission prep and is tightened as the system is commissioned; a one-way
    permanent lock at this stage would block the corrections ops still needs."""
    for model_name in ('PhysicsTag', 'EvgenTag', 'SimuTag', 'RecoTag', 'BackgroundTag'):
        apps.get_model('pcs', model_name).objects.update(status='draft')


class Migration(migrations.Migration):

    dependencies = [
        ('pcs', '0011_refresh_background_param_defs'),
    ]

    operations = [
        migrations.RunPython(unlock_all_tags, migrations.RunPython.noop),
    ]
