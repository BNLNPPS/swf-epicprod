from django.contrib import admin
from .models import (PhysicsCategory, PhysicsTag, EvgenTag, SimuTag, RecoTag, BackgroundTag,
                     Dataset, ProdConfig, ProdTask, PandaTasks,
                     Campaign, Questionnaire, ProdRequest)


@admin.register(PhysicsCategory)
class PhysicsCategoryAdmin(admin.ModelAdmin):
    list_display = ('digit', 'name', 'created_by', 'created_at')
    search_fields = ('name',)


@admin.register(PhysicsTag)
class PhysicsTagAdmin(admin.ModelAdmin):
    list_display = ('tag_label', 'category', 'status', 'description', 'created_by', 'created_at')
    list_filter = ('status', 'category')
    search_fields = ('tag_label', 'description')
    readonly_fields = ('tag_number', 'tag_label', 'created_at', 'updated_at')


@admin.register(EvgenTag)
class EvgenTagAdmin(admin.ModelAdmin):
    list_display = ('tag_label', 'status', 'description', 'created_by', 'created_at')
    list_filter = ('status',)
    search_fields = ('tag_label', 'description')
    readonly_fields = ('tag_number', 'tag_label', 'created_at', 'updated_at')


@admin.register(SimuTag)
class SimuTagAdmin(admin.ModelAdmin):
    list_display = ('tag_label', 'status', 'description', 'created_by', 'created_at')
    list_filter = ('status',)
    search_fields = ('tag_label', 'description')
    readonly_fields = ('tag_number', 'tag_label', 'created_at', 'updated_at')


@admin.register(RecoTag)
class RecoTagAdmin(admin.ModelAdmin):
    list_display = ('tag_label', 'status', 'description', 'created_by', 'created_at')
    list_filter = ('status',)
    search_fields = ('tag_label', 'description')
    readonly_fields = ('tag_number', 'tag_label', 'created_at', 'updated_at')


@admin.register(BackgroundTag)
class BackgroundTagAdmin(admin.ModelAdmin):
    list_display = ('tag_label', 'status', 'description', 'created_by', 'created_at')
    list_filter = ('status',)
    search_fields = ('tag_label', 'description')
    readonly_fields = ('tag_number', 'tag_label', 'created_at', 'updated_at')


@admin.register(Dataset)
class DatasetAdmin(admin.ModelAdmin):
    list_display = (
        'did', 'dataset_name', 'stage', 'is_external', 'source_kind',
        'block_num', 'blocks', 'file_count', 'created_by', 'created_at',
    )
    list_filter = ('detector_version', 'detector_config')
    search_fields = ('dataset_name', 'did')
    readonly_fields = ('created_at',)


@admin.register(ProdConfig)
class ProdConfigAdmin(admin.ModelAdmin):
    list_display = ('name', 'jug_xl_tag', 'target_hours_per_job', 'events_per_task',
                    'bg_mixing', 'use_rucio', 'created_by', 'updated_at')
    search_fields = ('name', 'description')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(ProdTask)
class ProdTaskAdmin(admin.ModelAdmin):
    list_display = ('name', 'status', 'campaign', 'requestor', 'priority',
                    'dataset', 'prod_config', 'created_by', 'updated_at')
    list_filter = ('status', 'campaign', 'pre_tdr_use', 'early_science_use',
                   'other_use', 'new_request')
    search_fields = ('name', 'description', 'requestor')
    readonly_fields = ('condor_command', 'panda_command', 'created_at', 'updated_at')


@admin.register(PandaTasks)
class PandaTasksAdmin(admin.ModelAdmin):
    list_display = ('prod_task', 'try_number', 'jedi_task_id', 'task_name',
                    'association_source', 'updated_at')
    list_filter = ('association_source',)
    search_fields = ('task_name', 'jedi_task_id', 'prod_task__name',
                     'prod_task__dataset__composed_name')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(Campaign)
class CampaignAdmin(admin.ModelAdmin):
    list_display = ('name', 'lifecycle', 'start_date', 'end_date',
                    'clone_of', 'created_by', 'updated_at')
    list_filter = ('lifecycle',)
    search_fields = ('name', 'description')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(ProdRequest)
class ProdRequestAdmin(admin.ModelAdmin):
    list_display = ('id', 'requestor', 'status', 'priority', 'nevents',
                    'new_request', 'pre_tdr_use', 'early_science_use',
                    'other_use', 'updated_at')
    list_filter = ('status', 'requestor', 'pre_tdr_use', 'early_science_use',
                   'other_use', 'new_request')
    search_fields = ('requestor', 'description', 'simu_path', 'source_url',
                     'source_row', 'rucio_source')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(Questionnaire)
class QuestionnaireAdmin(admin.ModelAdmin):
    list_display = ('id', 'submitted_at', 'status', 'nevents', 'created_by',
                    'updated_at')
    list_filter = ('status',)
    search_fields = ('description', 'repository', 'contact', 'nevents',
                     'benchmark', 'estimate')
    readonly_fields = ('created_at', 'updated_at')
