from django.db import migrations


def drop_background_param_defs(apps, schema_editor):
    """Drop the seeded background (k) tag param-defs so they re-seed from the
    updated schema. The earlier placeholder field set (source_sample, …) was
    replaced by bg_source/bg_mechanism/bg_generator; the param-defs are seeded
    once into PersistentState and would otherwise keep the stale fields."""
    PersistentState = apps.get_model('monitor_app', 'PersistentState')
    ps = PersistentState.objects.filter(id=1).first()
    if ps and isinstance(ps.state_data, dict) and 'pcs_param_defs_k' in ps.state_data:
        del ps.state_data['pcs_param_defs_k']
        ps.save(update_fields=['state_data'])


class Migration(migrations.Migration):

    dependencies = [
        ('pcs', '0010_add_background_tag'),
        ('monitor_app', '0015_persistentstate_and_more'),
    ]

    operations = [
        migrations.RunPython(drop_background_param_defs, migrations.RunPython.noop),
    ]
