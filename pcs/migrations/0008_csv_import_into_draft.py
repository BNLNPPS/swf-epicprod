from django.db import migrations


def csv_import_into_draft(apps, schema_editor):
    """Pull every imported catalog row into the editable production lifecycle.

    The CSV import exists to turn Sakib's static ``default_datasets.csv`` list
    into the living, actionable current-campaign catalog, so those rows belong
    in the buildable flow from the start. The legacy ``csv_import`` holding
    status (and the per-row Adopt step) are retired: status ``csv_import`` →
    ``draft``, owned by wenaus. ``past_output`` archives are untouched.

    Idempotent: re-running matches nothing once the flip has happened.
    """
    ProdTask = apps.get_model('pcs', 'ProdTask')
    ProdTask.objects.filter(status='csv_import').update(
        status='draft', created_by='wenaus',
    )


class Migration(migrations.Migration):

    dependencies = [
        ('pcs', '0007_alter_campaign_lifecycle_alter_prodrequest_status_and_more'),
    ]

    # No clean reverse: once flipped, an originally-csv_import row is
    # indistinguishable from any other draft, so reversal would be lossy.
    operations = [
        migrations.RunPython(csv_import_into_draft, migrations.RunPython.noop),
    ]
