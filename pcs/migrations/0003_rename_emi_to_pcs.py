"""
Rename EMI to PCS: rename database tables and migrate PersistentState keys.

PRE-REQUISITE: The deploy script updates django_migrations and django_content_type
to change app='emi' to app='pcs' BEFORE this migration runs. Without that step,
Django won't know 0001/0002 are already applied and will fail.
"""
from django.db import migrations


def migrate_persistent_state_keys(apps, schema_editor):
    """Rename emi_* keys to pcs_* in PersistentState."""
    PersistentState = apps.get_model('monitor_app', 'PersistentState')
    try:
        ps = PersistentState.objects.get(id=1)
    except PersistentState.DoesNotExist:
        return

    changed = False
    new_data = {}
    for key, value in ps.state_data.items():
        if key.startswith('emi_'):
            new_key = 'pcs_' + key[4:]
            new_data[new_key] = value
            changed = True
        else:
            new_data[key] = value

    if changed:
        ps.state_data = new_data
        ps.save(update_fields=['state_data'])


def reverse_persistent_state_keys(apps, schema_editor):
    """Reverse: rename pcs_* keys back to emi_*."""
    PersistentState = apps.get_model('monitor_app', 'PersistentState')
    try:
        ps = PersistentState.objects.get(id=1)
    except PersistentState.DoesNotExist:
        return

    changed = False
    new_data = {}
    for key, value in ps.state_data.items():
        if key.startswith('pcs_'):
            new_key = 'emi_' + key[4:]
            new_data[new_key] = value
            changed = True
        else:
            new_data[key] = value

    if changed:
        ps.state_data = new_data
        ps.save(update_fields=['state_data'])


class Migration(migrations.Migration):

    dependencies = [
        ('pcs', '0002_prodconfig'),
        ('monitor_app', '0015_persistentstate_and_more'),
    ]

    operations = [
        # Rename all emi_* tables to pcs_*
        migrations.RunSQL(
            sql="ALTER TABLE IF EXISTS emi_physics_category RENAME TO pcs_physics_category;",
            reverse_sql="ALTER TABLE IF EXISTS pcs_physics_category RENAME TO emi_physics_category;",
        ),
        migrations.RunSQL(
            sql="ALTER TABLE IF EXISTS emi_physics_tag RENAME TO pcs_physics_tag;",
            reverse_sql="ALTER TABLE IF EXISTS pcs_physics_tag RENAME TO emi_physics_tag;",
        ),
        migrations.RunSQL(
            sql="ALTER TABLE IF EXISTS emi_evgen_tag RENAME TO pcs_evgen_tag;",
            reverse_sql="ALTER TABLE IF EXISTS pcs_evgen_tag RENAME TO emi_evgen_tag;",
        ),
        migrations.RunSQL(
            sql="ALTER TABLE IF EXISTS emi_simu_tag RENAME TO pcs_simu_tag;",
            reverse_sql="ALTER TABLE IF EXISTS pcs_simu_tag RENAME TO emi_simu_tag;",
        ),
        migrations.RunSQL(
            sql="ALTER TABLE IF EXISTS emi_reco_tag RENAME TO pcs_reco_tag;",
            reverse_sql="ALTER TABLE IF EXISTS pcs_reco_tag RENAME TO emi_reco_tag;",
        ),
        migrations.RunSQL(
            sql="ALTER TABLE IF EXISTS emi_dataset RENAME TO pcs_dataset;",
            reverse_sql="ALTER TABLE IF EXISTS pcs_dataset RENAME TO emi_dataset;",
        ),
        migrations.RunSQL(
            sql="ALTER TABLE IF EXISTS emi_prod_config RENAME TO pcs_prod_config;",
            reverse_sql="ALTER TABLE IF EXISTS pcs_prod_config RENAME TO emi_prod_config;",
        ),

        # Migrate PersistentState keys (emi_next_* → pcs_next_*, emi_param_defs_* → pcs_param_defs_*)
        migrations.RunPython(
            migrate_persistent_state_keys,
            reverse_persistent_state_keys,
        ),
    ]
