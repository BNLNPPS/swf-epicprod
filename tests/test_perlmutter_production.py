"""The Perlmutter feeder's task shape and its one-node hold; no external writes."""
import unittest
from unittest.mock import patch

from swf_epicprod import perlmutter_production as prod
from swf_epicprod.production_es import apply, definition


def settings():
    stem = 'neutron_5GeV_3to50deg.pOP000001'
    return dict(prod.SHAPE, events=prod.EVENTS, input_events=prod.EVENTS,
                input_dataset='group.EIC:group.EIC.esplan.26.07.3.p1107.pOP000001',
                row=f'SINGLE/neutron/5GeV/3to50deg.pOP000001/{stem},steer,{prod.EVENTS},0000')


class ShapeTest(unittest.TestCase):
    def test_one_job_takes_the_whole_task(self):
        cfg = definition(settings())
        self.assertEqual(cfg['events'] // cfg['events_per_job'], 1)

    def test_job_carries_the_four_hour_shape(self):
        spec = apply({'outputs': [{'level': 'RECO', 'dataset': '/RECO/x'}],
                      'env': {'EVGEN_GENERATOR': 'particle_gun', 'EVGEN_INTERNAL': 'true'},
                      'containerImage': '/cvmfs/image', 'csvBase': 'c', 'outDS': 'o'}, settings())
        self.assertEqual((spec['nJobs'], spec['nCore'], spec['memory']), (1, 256, 1350))
        for switch in ('ES_SLOTS=128', 'ES_EVENTS_PER_UNIT=500', 'ES_CLOSE_S=120', 'ES_CLOSE_CONCURRENCY=4',
                       'ES_DEADLINE_S=14100', 'ES_MARGIN_S=600',
                       'EPICPROD_SWITCH_ES_BATCH_CHANNEL=1', 'EPICPROD_SWITCH_ES_COHORT_START=1',
                       'EPICPROD_SWITCH_ES_FAST_MERGE=1', 'EPICPROD_SWITCH_ES_RESIDENT_UNIT=1',
                       'EPICPROD_SWITCH_ES_RECO_POOL=48', 'EPICPROD_SWITCH_ES_RECO_ASYNC=1',
                       'EPICPROD_SWITCH_ES_REPORT_BARRIER=1'):
            self.assertIn(switch, spec['exec'])
        self.assertEqual(spec['env']['EPICPROD_SWITCH_RESIDENT_SIM'], '1')
        self.assertNotIn('ES_PREEMPT', spec['exec'])

    def test_a_trial_carries_its_cut_and_a_cut_needs_its_time(self):
        trial = dict(preempt_at_s=1200, preempt_pilot=True, preempt_first_job=True)
        spec = apply({'outputs': [{'level': 'RECO', 'dataset': '/RECO/x'}],
                      'env': {'EVGEN_GENERATOR': 'particle_gun', 'EVGEN_INTERNAL': 'true'},
                      'containerImage': '/cvmfs/image', 'csvBase': 'c', 'outDS': 'o'},
                     dict(settings(), **trial))
        for switch in ('ES_PREEMPT_AT_S=1200', 'ES_PREEMPT_PILOT=1', 'ES_PREEMPT_FIRST_JOB=1'):
            self.assertIn(switch, spec['exec'])
        with self.assertRaises(ValueError):
            definition(dict(settings(), preempt_pilot=True))

    def test_a_draft_takes_exactly_its_submission_trial_keys(self):
        class Task:
            saved = 0
            def save(self, update_fields):
                Task.saved += 1
        task = Task()
        task.overrides = {'event_service': dict(prod.SHAPE, preempt_at_s=1200, preempt_pilot=True)}
        prod.shaped(task, {})
        self.assertNotIn('preempt_at_s', task.overrides['event_service'])
        prod.shaped(task, {'preempt_at_s': 900})
        self.assertEqual(task.overrides['event_service']['preempt_at_s'], 900)
        prod.shaped(task, {'preempt_at_s': 900})
        self.assertEqual(Task.saved, 2)


class HoldTest(unittest.TestCase):
    def replenish(self, active=(), jobs=0, tasks=(), status='production', trial=None):
        def setting(key, default=None):
            return trial or {} if key == 'perlmutter_production.trial' else True
        with patch('monitor_app.models.SysConfig.get_setting', side_effect=setting), \
             patch('monitor_app.cached_product.get_product'), \
             patch('monitor_app.epicprod_logging.log_epicprod_action') as self.log, \
             patch('swf_epicprod.queues.entries',
                   return_value=[{'queue': prod.QUEUE, 'status': status, 'reason': 'r'}]), \
             patch.object(prod, 'task_inventory', return_value=(list(active), None)), \
             patch.object(prod, 'occupancy', return_value=(jobs, list(tasks))), \
             patch.object(prod, 'reserve_sample', side_effect=AssertionError('reserved')):
            return prod.replenish()

    def test_any_job_or_live_task_on_the_queue_holds(self):
        for kwargs in ({'active': [41300]}, {'jobs': 1}, {'tasks': [41262]}):
            with self.subTest(**kwargs):
                self.assertEqual(self.replenish(**kwargs)['outcome'], 'steady')

    def test_a_closed_queue_adds_nothing_and_posts_nothing(self):
        result = self.replenish(status='off')
        self.assertEqual((result['outcome'], result['reason']), ('closed', 'r'))
        self.assertFalse(self.log.call_args.kwargs['live_default'])

    def test_a_trial_goes_in_beside_the_running_work(self):
        result = self.replenish(jobs=1, tasks=[41299], trial={'preempt_at_s': 1200, 'other': 1})
        self.assertEqual(result['outcome'], 'held')
        self.assertIn('reserved', result['reason'])

    def test_an_empty_queue_reserves_a_sample(self):
        result = self.replenish()
        self.assertEqual(result['outcome'], 'held')
        self.assertIn('reserved', result['reason'])


if __name__ == '__main__':
    unittest.main()
