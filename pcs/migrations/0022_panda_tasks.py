# Generated for PCS PanDA task association history.

from django.db import migrations, models
import django.db.models.deletion


def backfill_panda_tasks(apps, schema_editor):
    ProdTask = apps.get_model('pcs', 'ProdTask')
    PandaTasks = apps.get_model('pcs', 'PandaTasks')
    qs = ProdTask.objects.select_related('dataset').exclude(panda_task_id__isnull=True)
    for task in qs.iterator():
        task_name = getattr(task.dataset, 'composed_name', '') or task.name
        if PandaTasks.objects.filter(task_name=task_name).exists():
            task_name = f"{task_name}.try1.pk{task.pk}"
        PandaTasks.objects.get_or_create(
            jedi_task_id=task.panda_task_id,
            defaults={
                'prod_task_id': task.pk,
                'try_number': 1,
                'task_name': task_name,
                'out_ds': task_name,
                'log_ds': f"{task_name}_log/",
                'association_source': 'legacy_panda_task_id',
                'match_reason': 'backfilled from ProdTask.panda_task_id',
            },
        )


def unbackfill_panda_tasks(apps, schema_editor):
    PandaTasks = apps.get_model('pcs', 'PandaTasks')
    PandaTasks.objects.filter(association_source='legacy_panda_task_id').delete()


class Migration(migrations.Migration):

    dependencies = [
        ('pcs', '0021_questionnaire_nevents_text'),
    ]

    operations = [
        migrations.CreateModel(
            name='PandaTasks',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('try_number', models.PositiveIntegerField(default=1)),
                ('jedi_task_id', models.BigIntegerField(blank=True, db_index=True, null=True)),
                ('task_name', models.CharField(max_length=300, unique=True)),
                ('out_ds', models.CharField(blank=True, default='', max_length=300)),
                ('log_ds', models.CharField(blank=True, default='', max_length=300)),
                ('site', models.CharField(blank=True, default='', max_length=100)),
                ('status_snapshot', models.CharField(blank=True, default='', max_length=50)),
                ('association_source', models.CharField(blank=True, default='', max_length=50)),
                ('match_reason', models.TextField(blank=True, default='')),
                ('metadata', models.JSONField(blank=True, default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('prod_task', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='panda_tasks', to='pcs.prodtask')),
            ],
            options={
                'db_table': 'pcs_panda_tasks',
                'ordering': ['prod_task', 'try_number'],
            },
        ),
        migrations.AddConstraint(
            model_name='pandatasks',
            constraint=models.UniqueConstraint(fields=('prod_task', 'try_number'), name='pcs_panda_tasks_unique_try'),
        ),
        migrations.AddConstraint(
            model_name='pandatasks',
            constraint=models.UniqueConstraint(condition=models.Q(('jedi_task_id__isnull', False)), fields=('jedi_task_id',), name='pcs_panda_tasks_unique_jedi_task'),
        ),
        migrations.RunPython(backfill_panda_tasks, unbackfill_panda_tasks),
    ]
