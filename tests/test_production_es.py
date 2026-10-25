"""Whole-task budgets and ordinary production identity survive ES translation."""
import unittest

from swf_epicprod.production_es import apply, definition


def settings():
    return dict(events=10000, input_events=10000, events_per_job=2000, unit_events=250, close_s=300,
                deadline_s=3600, margin_s=600, memory_mb_per_core=6000, slots='auto',
                input_dataset='group.EIC:group.EIC.production.input',
                row='SINGLE/neutron/5GeV/3to50deg.gES1/neutron_5GeV_3to50deg.gES1,steer,10000,0000')


def spec():
    return {'outDS': 'group.EIC.sample.10kevents', 'csvBase': 'sample.10kevents',
            'site': 'BNL_ePIC_GOOGLE_es', 'userName': 'wenaus',
            'prodSourceLabel': 'managed', 'processingType': 'epicproduction',
            'containerImage': '/cvmfs/eicweb/eic_xl:26.07.3-stable',
            'env': {'COPYRECO': 'true', 'COPYFULL': 'false',
                    'EVGEN_GENERATOR': 'particle_gun', 'EVGEN_INTERNAL': 'true'},
            'outputs': [{'level': 'RECO', 'dataset': '/RECO/26.07.3/sample/10kevents'}]}


class ProductionES(unittest.TestCase):
    def test_fast_close_is_opt_in_and_has_a_validated_batch_bound(self):
        ordinary = apply(spec(), settings())['exec']
        self.assertNotIn('EPICPROD_SWITCH_ES_FAST_MERGE', ordinary)
        self.assertNotIn('ES_CLOSE_MAX_FILES', ordinary)
        result = apply(spec(), dict(settings(), fast_merge=True, close_max_files=64))['exec']
        self.assertIn('EPICPROD_SWITCH_ES_FAST_MERGE=1', result)
        self.assertIn('ES_CLOSE_MAX_FILES=64', result)
        for changes in ({'fast_merge': '1'}, {'close_max_files': 0}, {'close_max_files': True}):
            with self.assertRaises(ValueError):
                definition(dict(settings(), **changes))

    def test_cohort_start_is_opt_in_for_the_commissioning_task(self):
        self.assertNotIn('EPICPROD_SWITCH_ES_COHORT_START', apply(spec(), settings())['exec'])
        result = apply(spec(), dict(settings(), parallel_start=True))
        self.assertIn('EPICPROD_SWITCH_ES_COHORT_START=1', result['exec'])
        with self.assertRaises(ValueError):
            definition(dict(settings(), parallel_start='1'))

    def test_budget_and_production_identity_are_preserved(self):
        result = apply(spec(), settings())
        self.assertEqual(result['nJobs'] * result['nEventsPerJob'], 10000)
        self.assertEqual(result['nJobs'], 5)
        self.assertTrue(result['fineGrainedProc'])
        self.assertEqual(result['site'], 'BNL_ePIC_GOOGLE_es')
        self.assertEqual(result['userName'], 'wenaus')
        self.assertEqual(result['prodSourceLabel'], 'managed')
        self.assertEqual(result['processingType'], 'epicproduction')
        self.assertIn('es-production', result['exec'])
        self.assertNotIn('CANARY_', result['exec'])
        self.assertNotIn('/TEST/', result['exec'])
        self.assertNotIn('trial', result)
        self.assertEqual(result['outputs'][0]['dataset'], '/RECO/26.07.3/sample/10kevents')

    def test_disagreeing_or_unbounded_work_is_refused(self):
        for changes in ({'events': 0}, {'events': 10001}, {'events': True},
                        {'row': 'DIS/x,hepmc3.tree.root,20000,0000'},
                        {'input_dataset': ''}, {'input_events': 9999}, {'margin_s': 3600}, {'slots': '0'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                definition(dict(settings(), **changes))

    def test_trial_or_unsupported_output_is_refused(self):
        for changes in ({'trial': {'outputRoot': 'TEST/trial/x'}},
                        {'outputs': [{'level': 'RECO', 'dataset': '/TEST/canary/x'}]},
                        {'env': {'COPYFULL': 'true'}}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                apply(dict(spec(), **changes), settings())
