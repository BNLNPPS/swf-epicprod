"""Tests for PCS command/task-param generation.

Uses lightweight fakes rather than Django fixtures — the builders are pure
mapping functions, so we do not need the ORM.
"""
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from pcs.commands import build_evgen_task_params, build_task_params
from pcs.models import Dataset


def _make_task():
    """Build a SimpleNamespace task matching the example in docs/JEDI_INTEGRATION.md."""
    physics_tag = SimpleNamespace(
        tag_label='p3001',
        parameters={'beam_energy_electron': 10, 'beam_energy_hadron': 100},
    )
    evgen_tag = SimpleNamespace(tag_label='e1', parameters={})
    simu_tag = SimpleNamespace(tag_label='s1', parameters={})
    reco_tag = SimpleNamespace(tag_label='r1', parameters={})

    task_name = 'group.EIC.26.02.0.epic_craterlake.p3001.e1.s1.r1'
    dataset = SimpleNamespace(
        scope='group.EIC',
        detector_version='26.02.0',
        detector_config='epic_craterlake',
        physics_tag=physics_tag,
        evgen_tag=evgen_tag,
        simu_tag=simu_tag,
        reco_tag=reco_tag,
        block_num=1,
        did=f'group.EIC:{task_name}.b1',
        task_name=task_name,
    )

    cfg = {
        'panda_site': 'BNL_EPIC_PROD_1',
        'panda_working_group': 'EIC',
        'container_image': 'docker://eicweb/jug_xl:26.02.0-stable',
        'jug_xl_tag': '26.02.0-stable',
        'target_hours_per_job': 2,
        'events_per_task': 1000,
        'use_rucio': True,
        'copy_reco': True,
        'copy_full': False,
        'copy_log': True,
        'bg_mixing': False,
        'data': {
            'prod_source_label': 'managed',
            'processing_type': 'epicproduction',
            'n_jobs': 10,
            'events_per_job': 100,
            'corecount': 1,
            'ram_count': 4000,
            'transformation': 'https://pandaserver-doma.cern.ch/trf/user/runGen-00-00-02',
        },
    }

    return SimpleNamespace(
        dataset=dataset,
        created_by='wenaus',
        get_effective_config=lambda: cfg,
    )


class BuildTaskParamsTest(TestCase):
    def test_matches_design_example(self):
        params = build_task_params(_make_task())

        # Identity
        self.assertEqual(params['taskName'],
                         'group.EIC.26.02.0.epic_craterlake.p3001.e1.s1.r1')
        self.assertEqual(params['userName'], 'wenaus')
        self.assertEqual(params['vo'], 'eic')
        self.assertEqual(params['workingGroup'], 'EIC')
        self.assertEqual(params['campaign'], '26.02.0')

        # Processing
        self.assertEqual(params['prodSourceLabel'], 'managed')
        self.assertEqual(params['taskType'], 'production')
        self.assertEqual(params['processingType'], 'epicproduction')
        self.assertEqual(params['taskPriority'], 900)

        # Executable
        self.assertEqual(params['transPath'],
                         'https://pandaserver-doma.cern.ch/trf/user/runGen-00-00-02')
        self.assertEqual(params['transUses'], '')
        self.assertEqual(params['transHome'], '')
        self.assertEqual(params['architecture'], '')
        self.assertEqual(params['container_name'],
                         'docker://eicweb/jug_xl:26.02.0-stable')

        # Splitting
        self.assertTrue(params['noInput'])
        self.assertEqual(params['nFiles'], 10)
        self.assertEqual(params['nFilesPerJob'], 1)
        self.assertEqual(params['nEventsPerJob'], 100)
        self.assertEqual(params['coreCount'], 1)
        self.assertEqual(params['ramCount'], 4000)
        self.assertEqual(params['ramUnit'], 'MBPerCore')
        self.assertEqual(params['walltime'], 7200)  # 2 hours

        # Site
        self.assertEqual(params['site'], 'BNL_EPIC_PROD_1')
        self.assertEqual(params['cloud'], 'EIC')

        # Log dataset
        self.assertEqual(params['log']['dataset'],
                         'group.EIC:group.EIC.26.02.0.epic_craterlake.p3001.e1.s1.r1.log')
        self.assertEqual(params['log']['param_type'], 'log')
        self.assertEqual(params['log']['type'], 'template')
        self.assertIn('.${SN}.log.tgz', params['log']['value'])

        # jobParameters
        constant_entry = params['jobParameters'][0]
        self.assertEqual(constant_entry['type'], 'constant')
        for expected in ('EBEAM=10', 'PBEAM=100',
                         'DETECTOR_VERSION=26.02.0',
                         'DETECTOR_CONFIG=epic_craterlake',
                         'JUG_XL_TAG=26.02.0-stable',
                         'COPYRECO=true', 'COPYFULL=false', 'COPYLOG=true',
                         './run.sh'):
            self.assertIn(expected, constant_entry['value'])

        output_entry = params['jobParameters'][1]
        self.assertEqual(output_entry['type'], 'template')
        self.assertEqual(output_entry['param_type'], 'output')
        self.assertEqual(output_entry['dataset'],
                         'group.EIC:group.EIC.26.02.0.epic_craterlake.p3001.e1.s1.r1')
        self.assertIn('.${SN}.root', output_entry['value'])
        self.assertEqual(output_entry['offset'], 1000)

    def test_defaults_when_data_missing(self):
        task = _make_task()
        # Strip data dict
        task.get_effective_config = lambda: {
            'panda_site': '',
            'panda_working_group': '',
            'container_image': '',
            'target_hours_per_job': None,
            'events_per_task': None,
            'use_rucio': False,
            'copy_reco': False,
            'copy_full': False,
            'copy_log': False,
            'bg_mixing': False,
            'data': {},
        }
        params = build_task_params(task)

        self.assertEqual(params['vo'], 'eic')
        self.assertEqual(params['workingGroup'], 'EIC')
        self.assertEqual(params['prodSourceLabel'], 'test')
        self.assertEqual(params['processingType'], 'epicproduction')
        self.assertEqual(params['coreCount'], 1)
        self.assertEqual(params['ramCount'], 2000)
        self.assertNotIn('walltime', params)
        self.assertNotIn('nFiles', params)
        self.assertNotIn('skipScout', params)
        self.assertNotIn('useRucio', params)

    @patch('pcs.services.fetch_jlab_rucio_did_files')
    def test_evgen_uses_composed_name_for_outds(self, fetch_files):
        fetch_files.return_value = [{
            'name': '/EVGEN/DIS/NC/sample/lAger3.6.1-1.0_run1.hepmc3',
        }]
        physics_tag = SimpleNamespace(
            tag_label='p3001',
            parameters={'beam_energy_electron': 10, 'beam_energy_hadron': 100},
        )
        evgen_tag = SimpleNamespace(tag_label='e1', parameters={})
        dataset = SimpleNamespace(
            scope='group.EIC',
            detector_version='26.02.0',
            detector_config='epic_craterlake',
            physics_tag=physics_tag,
            evgen_tag=evgen_tag,
            composed_name='group.EIC.26.02.0.epic_craterlake.p3001.e1.s1.r1.sample',
            build_dataset_name=lambda: 'group.EIC.26.02.0.epic_craterlake.p3001.e1.s1.r1.sample',
        )
        task = SimpleNamespace(
            composed_name=dataset.composed_name,
            dataset=dataset,
            inputs=[{'did': 'epic:/EVGEN/DIS/NC/sample'}],
            created_by='wenaus',
            get_effective_config=lambda: {
                'panda_site': 'BNL_OSG_PanDA_1',
                'panda_working_group': 'EIC',
                'container_image': '/cvmfs/singularity.opensciencegrid.org/eicweb/eic_xl:26.02.0-stable',
                'target_hours_per_job': 2,
                'copy_reco': True,
                'copy_full': False,
                'copy_log': True,
                'use_rucio': True,
                'bg_mixing': False,
                'data': {'events_per_job': 1},
            },
        )

        spec = build_evgen_task_params(task)

        self.assertEqual(spec['outDS'], dataset.composed_name)
        self.assertEqual(spec['csvBase'], dataset.composed_name)
        self.assertIn(dataset.composed_name, spec['exec'])

    @patch('pcs.services.fetch_jlab_rucio_did_files')
    def test_evgen_env_prefers_background_tag_and_sets_log_rse(self, fetch_files):
        fetch_files.return_value = [{'name': '/EVGEN/DIS/NC/sample/run1.hepmc3'}]
        physics_tag = SimpleNamespace(
            tag_label='p3001',
            parameters={'beam_energy_electron': 10, 'beam_energy_hadron': 110},
        )
        evgen_tag = SimpleNamespace(
            tag_label='e1',
            parameters={
                'signal_freq': '9',
                'signal_status': '9',
                'bg_tag_prefix': 'legacy/prefix',
                'bg_files': 'legacy.json',
            },
        )
        background_tag = SimpleNamespace(
            tag_label='k1',
            parameters={
                'signal_freq': '0',
                'signal_status': '0',
                'bg_tag_prefix': 'Bkg_Exact1S_2us_e_only/GoldCt/10um',
                'bg_files': 'synrad_egas.json',
            },
        )
        dataset = SimpleNamespace(
            scope='group.EIC',
            detector_version='26.06.0',
            detector_config='epic_craterlake',
            physics_tag=physics_tag,
            evgen_tag=evgen_tag,
            background_tag_id=1,
            background_tag=background_tag,
            composed_name='group.EIC.26.06.0.epic_craterlake.p3001.e1.s1.r1.k1.sample',
            build_dataset_name=lambda: 'unused',
        )
        task = SimpleNamespace(
            composed_name=dataset.composed_name,
            dataset=dataset,
            inputs=[{'did': 'epic:/EVGEN/DIS/NC/sample'}],
            created_by='wenaus',
            get_effective_config=lambda: {
                'panda_site': 'NERSC_Perlmutter_epic',
                'panda_working_group': 'EIC',
                'container_image': '/cvmfs/singularity.opensciencegrid.org/eicweb/eic_xl:26.06.0-stable',
                'target_hours_per_job': 4,
                'copy_reco': True,
                'copy_full': False,
                'copy_log': True,
                'use_rucio': True,
                'rucio_rse': 'BNL-XRD',
                'bg_mixing': True,
                'data': {'events_per_job': 100, 'log_rse': 'EIC-XRD-LOG'},
            },
        )

        env = build_evgen_task_params(task)['env']

        self.assertEqual(env['LOG_RSE'], 'EIC-XRD-LOG')
        self.assertEqual(env['OUT_RSE'], 'BNL-XRD')
        self.assertEqual(env['SIGNAL_FREQ'], '0')
        self.assertEqual(env['SIGNAL_STATUS'], '0')
        self.assertEqual(env['TAG_PREFIX'], 'Bkg_Exact1S_2us_e_only/GoldCt/10um')
        self.assertEqual(env['BG_FILES'], 'synrad_egas.json')

    @patch('pcs.services.fetch_jlab_rucio_did_files')
    def test_evgen_env_keeps_evgen_background_fallback(self, fetch_files):
        fetch_files.return_value = [{'name': '/EVGEN/DIS/NC/sample/run1.hepmc3'}]
        physics_tag = SimpleNamespace(
            tag_label='p3001',
            parameters={'beam_energy_electron': 10, 'beam_energy_hadron': 100},
        )
        evgen_tag = SimpleNamespace(
            tag_label='e1',
            parameters={
                'signal_freq': '2',
                'signal_status': '1',
                'bg_tag_prefix': 'legacy/prefix',
                'bg_files': 'legacy.json',
            },
        )
        dataset = SimpleNamespace(
            scope='group.EIC',
            detector_version='26.06.0',
            detector_config='epic_craterlake',
            physics_tag=physics_tag,
            evgen_tag=evgen_tag,
            background_tag_id=None,
            background_tag=None,
            composed_name='group.EIC.26.06.0.epic_craterlake.p3001.e1.s1.r1.sample',
            build_dataset_name=lambda: 'unused',
        )
        task = SimpleNamespace(
            composed_name=dataset.composed_name,
            dataset=dataset,
            inputs=[{'did': 'epic:/EVGEN/DIS/NC/sample'}],
            created_by='wenaus',
            get_effective_config=lambda: {
                'panda_site': 'BNL_OSG_PanDA_1',
                'panda_working_group': 'EIC',
                'container_image': '/cvmfs/singularity.opensciencegrid.org/eicweb/eic_xl:26.06.0-stable',
                'target_hours_per_job': 2,
                'copy_reco': True,
                'copy_full': False,
                'copy_log': True,
                'use_rucio': True,
                'bg_mixing': True,
                'data': {'events_per_job': 100},
            },
        )

        env = build_evgen_task_params(task)['env']

        self.assertEqual(env['SIGNAL_FREQ'], '2')
        self.assertEqual(env['SIGNAL_STATUS'], '1')
        self.assertEqual(env['TAG_PREFIX'], 'legacy/prefix')
        self.assertEqual(env['BG_FILES'], 'legacy.json')

    @patch('pcs.services.fetch_jlab_rucio_did_files')
    def test_evgen_try_rerun_adds_payload_tag_prefix(self, fetch_files):
        fetch_files.return_value = [{'name': '/EVGEN/DIS/NC/sample/run1.hepmc3'}]
        physics_tag = SimpleNamespace(
            tag_label='p3001',
            parameters={'beam_energy_electron': 10, 'beam_energy_hadron': 100},
        )
        dataset = SimpleNamespace(
            scope='group.EIC',
            detector_version='26.06.0',
            detector_config='epic_craterlake',
            physics_tag=physics_tag,
            evgen_tag=SimpleNamespace(parameters={}),
            background_tag_id=None,
            background_tag=None,
            composed_name='group.EIC.26.06.0.epic_craterlake.p3001.e1.s1.r1.sample',
            build_dataset_name=lambda: 'unused',
        )
        task = SimpleNamespace(
            composed_name=dataset.composed_name,
            dataset=dataset,
            inputs=[{'did': 'epic:/EVGEN/DIS/NC/sample'}],
            created_by='wenaus',
            get_effective_config=lambda: {
                'panda_site': 'BNL_OSG_PanDA_1',
                'panda_working_group': 'EIC',
                'container_image': '/cvmfs/singularity.opensciencegrid.org/eicweb/eic_xl:26.06.0-stable',
                'target_hours_per_job': 2,
                'copy_reco': True,
                'copy_full': False,
                'copy_log': True,
                'use_rucio': True,
                'bg_mixing': False,
                'data': {'events_per_job': 100},
            },
        )
        panda_tasks = SimpleNamespace(
            try_number=2,
            task_name=f'{dataset.composed_name}.try2',
        )

        env = build_evgen_task_params(task, panda_tasks=panda_tasks)['env']

        self.assertEqual(env['TAG_PREFIX'], 'try2')

    @patch('pcs.services.fetch_jlab_rucio_did_files')
    def test_evgen_try_rerun_appends_to_existing_payload_tag_prefix(self, fetch_files):
        fetch_files.return_value = [{'name': '/EVGEN/DIS/NC/sample/run1.hepmc3'}]
        physics_tag = SimpleNamespace(
            tag_label='p3001',
            parameters={'beam_energy_electron': 10, 'beam_energy_hadron': 100},
        )
        evgen_tag = SimpleNamespace(
            parameters={'bg_tag_prefix': 'legacy/prefix'},
        )
        dataset = SimpleNamespace(
            scope='group.EIC',
            detector_version='26.06.0',
            detector_config='epic_craterlake',
            physics_tag=physics_tag,
            evgen_tag=evgen_tag,
            background_tag_id=None,
            background_tag=None,
            composed_name='group.EIC.26.06.0.epic_craterlake.p3001.e1.s1.r1.sample',
            build_dataset_name=lambda: 'unused',
        )
        task = SimpleNamespace(
            composed_name=dataset.composed_name,
            dataset=dataset,
            inputs=[{'did': 'epic:/EVGEN/DIS/NC/sample'}],
            created_by='wenaus',
            get_effective_config=lambda: {
                'panda_site': 'BNL_OSG_PanDA_1',
                'panda_working_group': 'EIC',
                'container_image': '/cvmfs/singularity.opensciencegrid.org/eicweb/eic_xl:26.06.0-stable',
                'target_hours_per_job': 2,
                'copy_reco': True,
                'copy_full': False,
                'copy_log': True,
                'use_rucio': True,
                'bg_mixing': True,
                'data': {'events_per_job': 100},
            },
        )
        panda_tasks = SimpleNamespace(
            try_number=3,
            task_name=f'{dataset.composed_name}.try3',
        )

        env = build_evgen_task_params(task, panda_tasks=panda_tasks)['env']

        self.assertEqual(env['TAG_PREFIX'], 'legacy/prefix/try3')


class DatasetMetadataTest(TestCase):
    def test_external_evgen_metadata_helpers(self):
        metadata = Dataset.external_evgen_metadata(
            source_location='campaign/input.csv',
        )
        dataset = Dataset(metadata=metadata)

        self.assertEqual(dataset.stage, 'evgen')
        self.assertTrue(dataset.is_external)
        self.assertEqual(dataset.source_kind, 'csv_manifest')
        self.assertEqual(dataset.source_location, 'campaign/input.csv')
        self.assertEqual(dataset.validation_status, '')

    def test_metadata_helpers_tolerate_null_metadata(self):
        dataset = Dataset(metadata=None)

        self.assertEqual(dataset.stage, '')
        self.assertFalse(dataset.is_external)
        self.assertEqual(dataset.source_kind, '')
        self.assertEqual(dataset.source_location, '')
