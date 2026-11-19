"""Analysis runs in the database: dataset cards from PCS's delivered
outputs, tags, freezing, the PanDA specifications and the report
endpoints (docs/ANALYSIS.md)."""
from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.test import APIClient

from pcs.models import (Campaign, Dataset, DeliveredOutput, EvgenTag, PandaTasks,
                        PhysicsCategory, PhysicsTag, ProdConfig, ProdTask, RecoTag,
                        SimuTag)

from . import cards, services
from .models import AnalysisJobOutput, AnalysisRun, AnalysisTag

SAMPLE = 'RECO/26.07.3/epic_craterlake/SIDIS/pythia6-lhapdf/1.1.0/ep_noradcor/9x130/x_1e-2to1e-1'
STEM = 'pythia6-lhapdf_1.1.0_ep_noradcor_9x130_x_1e-2to1e-1_run001.ab'
COMMIT = '8ccc766ecdb7d2b408d882630248ba77ea357a73'
CARD_TEXT = """\
identifier: "epic:benchmark:SIDIS/dihadron"
description: dihadron
eic_shell:
  container: eic_xl
  version: 26.07.3-stable
parameters:
  DIHADRON_TRIG_LOW: 2
  MODE: reco
split:
  files_per_job: 2
stages:
  map: 'snakemake --cores 1 --config $PARAMS -- results/dihadron/$SAMPLE/$MODE/job_$JOB.root'
  merge: 'snakemake --cores 1 --config $PARAMS -- results/dihadron/$SAMPLE/$MODE/merged.root'
  finalize: 'snakemake --cores 1 --config $PARAMS -- results/dihadron/$SAMPLE/$MODE/plots'
files:
  list: results/dihadron/$SAMPLE/lists/list_$JOB.txt
  partial: results/dihadron/$SAMPLE/$MODE/job_$JOB.root
  hists: results/dihadron/$SAMPLE/$MODE/hists.json
  plots: results/dihadron/$SAMPLE/$MODE/plots
"""


def reco(unit, attempt=1):
    path = SAMPLE if attempt == 1 else SAMPLE.replace('craterlake/', f'craterlake/try{attempt}/')
    return f'/{path}/{STEM}.{unit}.eicrecon.edm4eic.root'


class AnalysisFixture(TestCase):

    def setUp(self):
        category = PhysicsCategory.objects.create(digit=4, name='SIDIS', created_by='t')
        tags = dict(
            physics_tag=PhysicsTag.objects.create(tag_number=4001, tag_label='', category=category,
                                                  parameters={}, created_by='t'),
            evgen_tag=EvgenTag.objects.create(tag_number=1, tag_label='', created_by='t'),
            simu_tag=SimuTag.objects.create(tag_number=1, tag_label='', created_by='t'),
            reco_tag=RecoTag.objects.create(tag_number=1, tag_label='', created_by='t'))
        campaign = Campaign.objects.create(name='26.07', lifecycle='past', created_by='t')
        self.dataset = Dataset.objects.create(
            dataset_name='dihadron.ep', did='group.EIC:dihadron.ep', detector_version='26.07.3',
            detector_config='epic_craterlake', campaign=campaign, created_by='t', **tags)
        task = ProdTask.objects.create(name='t', status='submitted', dataset=self.dataset,
                                       prod_config=ProdConfig.objects.create(name='c', created_by='t'),
                                       campaign=campaign, created_by='t')
        self.attempts = {n: PandaTasks.objects.create(prod_task=task, try_number=n,
                                                      jedi_task_id=1000 + n, task_name=f't{n}')
                         for n in (1, 2)}
        self.task = task

    def deliver(self, unit, attempt=1, status='delivered', events=100):
        DeliveredOutput.objects.create(
            prod_task=self.task, panda_task=self.attempts[attempt], pandaid=1,
            did=reco(unit, attempt), kind='RECO', segment=unit, events=events,
            bytes=10, status=status)

    def frozen_run(self, units=5, **kw):
        for k in range(units):
            self.deliver(f'{k:04d}.0001')
        card, _ = services.freeze_dataset_card(self.dataset, SAMPLE, 'tester')
        tag, _ = services.register_tag('https://github.com/eic/physics_benchmarks', COMMIT,
                                       'benchmarks/SIDIS/dihadron/card.yml', CARD_TEXT, 'tester')
        run = AnalysisRun.objects.create(tag=tag, owner='rahmans', **kw)
        run.inputs.add(card)
        run, _ = services.freeze_run(run)
        return run


class DatasetCardTest(AnalysisFixture):

    def test_card_keeps_latest_delivered_try(self):
        self.deliver('0001.0001', 1)
        self.deliver('0001.0001', 2, events=90)
        self.deliver('0002.0001', 1)
        self.deliver('0003.0001', 1, status='lost')
        card, created = services.freeze_dataset_card(self.dataset, SAMPLE, 'tester')
        self.assertTrue(created)
        self.assertEqual([r['did'] for r in card.rows()],
                         [reco('0001.0001', 2), reco('0002.0001', 1)])
        self.assertEqual(card.events, 190)
        self.assertEqual(card.revision, 1)

    def test_same_delivery_same_revision_new_delivery_next(self):
        self.deliver('0001.0001')
        first, _ = services.freeze_dataset_card(self.dataset, SAMPLE, 'tester')
        again, created = services.freeze_dataset_card(self.dataset, SAMPLE, 'tester')
        self.assertEqual((again.pk, created), (first.pk, False))
        self.deliver('0002.0001')
        second, created = services.freeze_dataset_card(self.dataset, SAMPLE, 'tester')
        self.assertEqual((second.revision, created), (2, True))

    def test_served_without_login(self):
        self.deliver('0001.0001')
        services.freeze_dataset_card(self.dataset, SAMPLE, 'tester')
        response = APIClient().get('/analysis/api/v1/dataset-card/', {'sample': SAMPLE})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['file_count'], 1)
        blob = APIClient().get('/analysis/api/v1/dataset-card/',
                               {'sample': SAMPLE, 'manifest': 1}).content
        self.assertEqual(cards.read_manifest_blob(blob)[0]['did'], reco('0001.0001'))


class TagTest(AnalysisFixture):

    def test_register_reads_the_card_and_reuses_a_pin(self):
        tag, created = services.register_tag('https://github.com/eic/physics_benchmarks', COMMIT,
                                             'benchmarks/SIDIS/dihadron/card.yml', CARD_TEXT, 't')
        self.assertTrue(created)
        self.assertEqual(tag.label, 'a1')
        self.assertEqual(tag.image, '/cvmfs/singularity.opensciencegrid.org/eicweb/eic_xl:26.07.3-stable')
        again, created = services.register_tag('https://github.com/eic/physics_benchmarks', COMMIT,
                                               'benchmarks/SIDIS/dihadron/card.yml', CARD_TEXT, 't')
        self.assertEqual((again.pk, created), (tag.pk, False))

    def test_a_used_tag_cannot_change(self):
        run = self.frozen_run()
        tag = run.tag
        tag.commit = '0' * 40
        with self.assertRaises(cards.CardError):
            tag.save()
        tag.refresh_from_db()
        tag.description = 'notes may change'
        tag.save()


class RunTest(AnalysisFixture):

    def test_freeze_writes_card_and_campaign(self):
        run = self.frozen_run()
        self.assertEqual(run.status, 'frozen')
        self.assertEqual(run.campaign, '26073')
        self.assertEqual(run.run_card_id, cards.run_card_id(run.run_card))
        self.assertEqual(run.run_card['parameters'], {'DIHADRON_TRIG_LOW': 2, 'MODE': 'reco'})

    def test_the_same_computation_is_one_run(self):
        run = self.frozen_run()
        twin = AnalysisRun.objects.create(tag=run.tag, owner='someone', purpose='validation')
        twin.inputs.add(*run.inputs.all())
        same, frozen_now = services.freeze_run(twin)
        self.assertEqual((same.pk, frozen_now), (run.pk, False))

    def test_map_spec(self):
        run = self.frozen_run(units=5)
        spec = services.build_map_spec(run)
        self.assertEqual(spec['nJobs'], 3)
        self.assertEqual(spec['prodSourceLabel'], 'user')
        self.assertEqual(spec['memory'], 2000)
        self.assertEqual(spec['env']['ANALYSIS_COMMIT'], COMMIT)
        self.assertEqual(spec['env']['ANALYSIS_PARAMS'], 'DIHADRON_TRIG_LOW=2 MODE=reco')
        self.assertTrue(spec['env']['ANALYSIS_SCRATCH'].startswith('/rahmans/scratch/'))
        first = cards.parse_job_row(spec['csvRows'][0])
        self.assertEqual(first['dids'], [reco('0000.0001'), reco('0001.0001')])
        self.assertIn('%RNDM=0', spec['exec'])

    def test_merge_waits_for_every_partial_then_ships_the_record(self):
        run = self.frozen_run(units=3)
        scratch = spec_scratch = services.build_map_spec(run)['env']['ANALYSIS_SCRATCH']
        services.record_job_outputs(run, [{'seq': 0, 'did': f'{scratch}/a.job_0000.root'}])
        with self.assertRaises(cards.CardError):
            services.build_merge_spec(run)
        services.record_job_outputs(run, [{'seq': 1, 'did': f'{spec_scratch}/a.job_0001.root'}])
        spec = services.build_merge_spec(run)
        self.assertEqual(spec['nJobs'], 1)
        self.assertTrue(spec['csvRows'][0].startswith('0;ep_noradcor_9x130_x_1e-2to1e-1;merge;0000='))
        self.assertIn('analysis_run_card.json', spec['sandboxFiles'])


class ReportApiTest(AnalysisFixture):

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.client.force_authenticate(User.objects.create(username='ops'))

    def test_job_outputs_are_idempotent_and_confined(self):
        run = self.frozen_run(units=3)
        scratch = services.build_map_spec(run)['env']['ANALYSIS_SCRATCH']
        url = f'/analysis/api/v1/runs/{run.run_card_id}/job-outputs/'
        body = {'outputs': [{'seq': 0, 'did': f'{scratch}/x.root', 'adler32': '0a0b0c0d'},
                            {'seq': 7, 'did': f'{scratch}/y.root'},
                            {'seq': 1, 'did': '/elsewhere/z.root'}]}
        first = self.client.post(url, body, format='json').json()
        self.assertEqual(first['written'], 1)
        self.assertEqual(len(first['refused']), 2)
        self.client.post(url, body, format='json')
        self.assertEqual(AnalysisJobOutput.objects.filter(run=run).count(), 1)

    def test_writes_need_a_login(self):
        run = self.frozen_run()
        response = APIClient().post(f'/analysis/api/v1/runs/{run.run_card_id}/job-outputs/',
                                    {'outputs': [{'seq': 0}]}, format='json')
        self.assertIn(response.status_code, (401, 403))

    def test_result_completes_the_run(self):
        run = self.frozen_run(units=1)
        url = f'/analysis/api/v1/runs/{run.run_card_id}/result/'
        response = self.client.post(url, {'run_id': run.run_card_id,
                                          'sample': 'ep_noradcor_9x130_x_1e-2to1e-1'},
                                    format='json')
        self.assertEqual(response.json()['status'], 'done')
