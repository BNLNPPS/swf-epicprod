from django import forms
from .models import PhysicsCategory, PhysicsTag, EvgenTag, SimuTag, RecoTag, BackgroundTag, Dataset, ProdConfig
from .schemas import TAG_SCHEMAS, get_param_defs


class PhysicsCategoryForm(forms.ModelForm):
    class Meta:
        model = PhysicsCategory
        fields = ['digit', 'name', 'description', 'created_by']
        widgets = {
            'description': forms.Textarea(attrs={'rows': 3}),
        }


class PhysicsTagForm(forms.Form):
    category = forms.ModelChoiceField(
        queryset=PhysicsCategory.objects.all(),
        empty_label="Select category",
    )
    description = forms.CharField(widget=forms.Textarea(attrs={'rows': 3}))
    created_by = forms.CharField(max_length=100)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for pdef in get_param_defs('p'):
            self.fields[f'param_{pdef["name"]}'] = forms.CharField(
                label=pdef['name'], required=pdef.get('required', False),
            )

    def get_parameters(self):
        return {
            k.removeprefix('param_'): v
            for k, v in self.cleaned_data.items()
            if k.startswith('param_') and v
        }


class SimpleTagForm(forms.Form):
    description = forms.CharField(widget=forms.Textarea(attrs={'rows': 3}))
    created_by = forms.CharField(max_length=100)

    def __init__(self, *args, tag_type=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.tag_type = tag_type
        if tag_type:
            for pdef in get_param_defs(tag_type):
                self.fields[f'param_{pdef["name"]}'] = forms.CharField(
                    label=pdef['name'], required=pdef.get('required', False),
                )

    def get_parameters(self):
        return {
            k.removeprefix('param_'): v
            for k, v in self.cleaned_data.items()
            if k.startswith('param_') and v
        }


class DatasetForm(forms.Form):
    scope = forms.CharField(max_length=100, initial='group.EIC')
    detector_version = forms.CharField(max_length=50)
    detector_config = forms.CharField(max_length=100)
    physics_tag = forms.ModelChoiceField(
        queryset=PhysicsTag.objects.all(),
        empty_label="Select physics tag",
    )
    evgen_tag = forms.ModelChoiceField(
        queryset=EvgenTag.objects.all(),
        empty_label="Select evgen tag",
    )
    simu_tag = forms.ModelChoiceField(
        queryset=SimuTag.objects.all(),
        empty_label="Select simu tag",
    )
    reco_tag = forms.ModelChoiceField(
        queryset=RecoTag.objects.all(),
        empty_label="Select reco tag",
    )
    background_tag = forms.ModelChoiceField(
        queryset=BackgroundTag.objects.all(),
        empty_label="Select background tag (optional)",
        required=False,
    )
    description = forms.CharField(widget=forms.Textarea(attrs={'rows': 3}), required=False)
    metadata = forms.JSONField(required=False)
    created_by = forms.CharField(max_length=100)


class ProdConfigForm(forms.ModelForm):
    class Meta:
        model = ProdConfig
        fields = [
            'name', 'description',
            'bg_mixing', 'bg_cross_section', 'bg_evtgen_file',
            'copy_reco', 'copy_full', 'copy_log', 'use_rucio',
            'jug_xl_tag', 'container_image',
            'target_hours_per_job', 'events_per_task',
            'condor_template',
            'panda_site', 'panda_queue', 'panda_working_group', 'panda_resource_type',
            'rucio_rse', 'rucio_replication_rules',
            'data',
            'created_by',
        ]
        widgets = {
            'description': forms.Textarea(attrs={'rows': 3}),
            'condor_template': forms.Textarea(attrs={'rows': 10, 'style': 'font-family: monospace;'}),
            'rucio_replication_rules': forms.Textarea(attrs={'rows': 4, 'style': 'font-family: monospace;'}),
            'data': forms.Textarea(attrs={'rows': 8, 'style': 'font-family: monospace;',
                                          'placeholder': '{\n  "transformation": "runGen-00-00-02",\n  "processing_type": "epicproduction",\n  "n_jobs": 1000,\n  "events_per_job": 100,\n  "corecount": 1\n}'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs.setdefault('class', 'form-check-input')
            else:
                field.widget.attrs.setdefault('class', 'form-control')
