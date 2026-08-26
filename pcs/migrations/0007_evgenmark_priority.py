from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('pcs', '0006_evgenmark'),
    ]

    operations = [
        migrations.AddField(
            model_name='evgenmark',
            name='priority',
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='evgenmark',
            name='priority_set_by',
            field=models.CharField(blank=True, default='', max_length=150),
        ),
        migrations.AddField(
            model_name='evgenmark',
            name='priority_set_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
