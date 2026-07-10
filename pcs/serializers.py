import re

from rest_framework import serializers
from .models import (PhysicsCategory, PhysicsTag, EvgenTag, SimuTag, RecoTag, BackgroundTag,
                     Dataset, ProdConfig, ProdTask, PandaTasks, Questionnaire)
from .schemas import validate_parameters


class PhysicsCategorySerializer(serializers.ModelSerializer):
    tag_count = serializers.IntegerField(read_only=True, required=False)

    class Meta:
        model = PhysicsCategory
        fields = ['digit', 'name', 'description', 'created_by', 'created_at', 'tag_count']
        read_only_fields = ['created_by', 'created_at']


class PhysicsTagSerializer(serializers.ModelSerializer):
    category_name = serializers.CharField(source='category.name', read_only=True)

    class Meta:
        model = PhysicsTag
        fields = [
            'id', 'tag_number', 'tag_label', 'category', 'category_name',
            'status', 'description', 'parameters',
            'created_by', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'tag_number', 'tag_label', 'status', 'created_by', 'created_at', 'updated_at']

    def validate_parameters(self, value):
        ok, msg = validate_parameters('p', value)
        if not ok:
            raise serializers.ValidationError(msg)
        return value


class _SimpleTagSerializer(serializers.ModelSerializer):
    class Meta:
        fields = [
            'id', 'tag_number', 'tag_label', 'status', 'description',
            'parameters', 'created_by', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'tag_number', 'tag_label', 'status', 'created_by', 'created_at', 'updated_at']


class EvgenTagSerializer(_SimpleTagSerializer):
    class Meta(_SimpleTagSerializer.Meta):
        model = EvgenTag

    def validate_parameters(self, value):
        ok, msg = validate_parameters('e', value)
        if not ok:
            raise serializers.ValidationError(msg)
        return value


class SimuTagSerializer(_SimpleTagSerializer):
    class Meta(_SimpleTagSerializer.Meta):
        model = SimuTag

    def validate_parameters(self, value):
        ok, msg = validate_parameters('s', value)
        if not ok:
            raise serializers.ValidationError(msg)
        return value


class RecoTagSerializer(_SimpleTagSerializer):
    class Meta(_SimpleTagSerializer.Meta):
        model = RecoTag

    def validate_parameters(self, value):
        ok, msg = validate_parameters('r', value)
        if not ok:
            raise serializers.ValidationError(msg)
        return value


class BackgroundTagSerializer(_SimpleTagSerializer):
    class Meta(_SimpleTagSerializer.Meta):
        model = BackgroundTag

    def validate_parameters(self, value):
        ok, msg = validate_parameters('k', value)
        if not ok:
            raise serializers.ValidationError(msg)
        return value


class DatasetSerializer(serializers.ModelSerializer):
    physics_tag_label = serializers.CharField(source='physics_tag.tag_label', read_only=True)
    evgen_tag_label = serializers.CharField(source='evgen_tag.tag_label', read_only=True)
    simu_tag_label = serializers.CharField(source='simu_tag.tag_label', read_only=True)
    reco_tag_label = serializers.CharField(source='reco_tag.tag_label', read_only=True)
    background_tag_label = serializers.CharField(source='background_tag.tag_label', read_only=True, allow_null=True)
    stage = serializers.CharField(read_only=True)
    external = serializers.BooleanField(source='is_external', read_only=True)
    source_kind = serializers.CharField(read_only=True)
    source_location = serializers.CharField(read_only=True)
    validation_status = serializers.CharField(read_only=True)

    class Meta:
        model = Dataset
        fields = [
            'id', 'dataset_name', 'scope', 'detector_version', 'detector_config',
            'physics_tag', 'evgen_tag', 'simu_tag', 'reco_tag', 'background_tag',
            'physics_tag_label', 'evgen_tag_label', 'simu_tag_label', 'reco_tag_label',
            'background_tag_label',
            'block_num', 'blocks', 'did', 'file_count', 'data_size',
            'stage', 'external', 'source_kind', 'source_location', 'validation_status',
            'description', 'metadata', 'created_by', 'created_at',
        ]
        read_only_fields = [
            'id', 'dataset_name', 'did', 'block_num', 'blocks',
            'file_count', 'data_size', 'created_by', 'created_at',
        ]

    def validate_metadata(self, value):
        if value in (None, ''):
            return None
        if not isinstance(value, dict):
            raise serializers.ValidationError('metadata must be a JSON object.')
        if 'external' in value:
            raise serializers.ValidationError(
                'metadata.external is obsolete; external status is derived from metadata.source.kind.'
            )
        source = value.get('source')
        if source is not None:
            if not isinstance(source, dict):
                raise serializers.ValidationError('metadata.source must be a JSON object.')
            if not source.get('kind'):
                raise serializers.ValidationError('external datasets require metadata.source.kind.')
            if not source.get('location'):
                raise serializers.ValidationError('external datasets require metadata.source.location.')
        return value


class ProdConfigSerializer(serializers.ModelSerializer):
    workflow_mode = serializers.CharField(read_only=True)

    class Meta:
        model = ProdConfig
        fields = '__all__'
        read_only_fields = ['id', 'created_by', 'created_at', 'updated_at']


class PandaTasksSerializer(serializers.ModelSerializer):
    current = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = PandaTasks
        fields = [
            'id', 'try_number', 'jedi_task_id', 'task_name', 'out_ds', 'log_ds',
            'site', 'status_snapshot', 'association_source', 'match_reason',
            'metadata', 'current', 'created_at', 'updated_at',
        ]
        read_only_fields = fields

    def get_current(self, obj):
        task = getattr(obj, 'prod_task', None)
        return bool(task and task.panda_task_id and obj.jedi_task_id == task.panda_task_id)


def _redact_contact(value):
    value = (value or '').strip()
    if not value:
        return ''
    email_pattern = r'([A-Za-z0-9._%+-]+)@[A-Za-z0-9.-]+'
    email = re.fullmatch(email_pattern, value)
    if email:
        return email.group(1)
    value = re.sub(email_pattern, '', value).replace('<', ' ').replace('>', ' ')
    parts = [p for p in value.replace(',', ' ').split() if p]
    initials = ''.join(p[0].upper() for p in parts if p[0].isalpha())
    return initials or ''


class QuestionnaireSerializer(serializers.ModelSerializer):
    contact = serializers.SerializerMethodField()
    contact_redacted = serializers.SerializerMethodField()

    class Meta:
        model = Questionnaire
        fields = [
            'id', 'submitted_at', 'description', 'repository', 'contact',
            'contact_redacted', 'nevents', 'benchmark', 'estimate',
            'status', 'source_url', 'source_row', 'data',
            'created_by', 'created_at', 'updated_at',
        ]
        read_only_fields = fields

    def _authenticated(self):
        request = self.context.get('request')
        return bool(request and request.user and request.user.is_authenticated)

    def get_contact(self, obj):
        if self._authenticated():
            return obj.contact
        return _redact_contact(obj.contact)

    def get_contact_redacted(self, obj):
        return not self._authenticated()


class ProdTaskSerializer(serializers.ModelSerializer):
    composed_name = serializers.CharField(read_only=True)
    dataset_name = serializers.CharField(source='dataset.dataset_name', read_only=True)
    dataset_did = serializers.CharField(source='dataset.did', read_only=True)
    prod_config_name = serializers.CharField(source='prod_config.name', read_only=True)
    input_dataset_dids = serializers.SerializerMethodField(read_only=True)
    output_dataset_dids = serializers.SerializerMethodField(read_only=True)
    intermediate_dataset_dids = serializers.SerializerMethodField(read_only=True)
    panda_tasks = PandaTasksSerializer(many=True, read_only=True)

    class Meta:
        model = ProdTask
        fields = [
            'id', 'name', 'composed_name', 'description', 'status',
            'dataset', 'dataset_name', 'dataset_did',
            'prod_config', 'prod_config_name',
            'csv_file', 'overrides',
            'input_dataset_dids', 'output_dataset_dids', 'intermediate_dataset_dids',
            'condor_command', 'panda_command',
            'panda_task_id', 'panda_tasks', 'condor_cluster_id',
            'created_by', 'created_at', 'updated_at',
        ]
        read_only_fields = [
            'id', 'condor_command', 'panda_command',
            'input_dataset_dids', 'output_dataset_dids', 'intermediate_dataset_dids',
            'created_by', 'created_at', 'updated_at',
        ]

    def get_input_dataset_dids(self, obj):
        return [d.did for d in obj.input_datasets]

    def get_output_dataset_dids(self, obj):
        return [d.did for d in obj.output_dataset_overrides]

    def get_intermediate_dataset_dids(self, obj):
        return [d.did for d in obj.intermediate_datasets]
