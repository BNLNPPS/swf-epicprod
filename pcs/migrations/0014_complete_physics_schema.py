from django.db import migrations


def fill_background_category_description(apps, schema_editor):
    """The Background category (digit 6) was seeded without a description, unlike
    categories 1-5. Fill it."""
    PhysicsCategory = apps.get_model('pcs', 'PhysicsCategory')
    PhysicsCategory.objects.filter(digit=6, description='').update(
        description='Background k tags compose with physics tags they overlay'
    )


def drop_physics_param_defs(apps, schema_editor):
    """Drop the cached physics (p) tag param-defs so they re-seed from the
    completed schema (new fields: nucleon, helicity, beam_config, state,
    mechanism, final_state, channel, mass; new process values). Mirrors
    0011's background-param-defs refresh: the defs are seeded once into
    PersistentState and otherwise keep the stale field set."""
    PersistentState = apps.get_model('monitor_app', 'PersistentState')
    ps = PersistentState.objects.filter(id=1).first()
    if ps and isinstance(ps.state_data, dict) and 'pcs_param_defs_p' in ps.state_data:
        del ps.state_data['pcs_param_defs_p']
        ps.save(update_fields=['state_data'])


class Migration(migrations.Migration):

    dependencies = [
        ('pcs', '0013_fix_seed_created_by'),
        ('monitor_app', '0015_persistentstate_and_more'),
    ]

    operations = [
        migrations.RunPython(fill_background_category_description, migrations.RunPython.noop),
        migrations.RunPython(drop_physics_param_defs, migrations.RunPython.noop),
    ]
