"""The pilot regulator's decision, over a fake harvester reading and
census (swf_epicprod.pilots.decide is pure)."""
import unittest
from datetime import datetime, timedelta, timezone

from swf_epicprod.pilots import decide, harvester_reading

NOW = datetime(2026, 10, 7, 7, 0, tzinfo=timezone.utc)
IN_FORCE = {'maxWorkers': 5000, 'nQueueLimitWorkerMax': 5000, 'nQueueLimitJobMax': 2000}


def reading(running=540, queued=1295, ended=200, unfinished=0, age=60, in_force=None):
    return {'host': 'pandaharvester01', 'report_age_s': age, 'interval_s': 300,
            'running': running, 'queued': queued, 'ended': ended,
            'ended_unfinished': unfinished, 'in_force': dict(in_force or IN_FORCE),
            'db_error': None}


def census(activated=14000, p90_h=1.0):
    return {'by_status': {'activated': {'jobs': activated}},
            'calibration': {'p90_start_latency_h': p90_h}}


TARGET = {'target_running': 1000, 'enabled': False}


class PilotDecide(unittest.TestCase):

    def test_regulates_to_target(self):
        # 200 ended in 300 s: 2,400 starts an hour, so Q is capped at T.
        state, reason, rec = decide('Q', reading(), census(), [], TARGET,
                                    mode='shadow', now=NOW)
        self.assertEqual((state, reason), ('would_set', 'regulate'))
        self.assertEqual(rec['queued_target'], 1000)
        self.assertEqual(rec['would_set']['maxWorkers'], 2000)
        self.assertEqual(rec['would_set']['nQueueLimitWorkerMax'], 1000)
        self.assertEqual(rec['would_set']['nQueueLimitJobMax'], 1100)
        self.assertEqual(rec['would_set']['maxNewWorkersPerCycle'], 100)

    def test_raise_is_bounded(self):
        low = {'maxWorkers': 200, 'nQueueLimitWorkerMax': 100, 'nQueueLimitJobMax': 100}
        _, _, rec = decide('Q', reading(queued=50, in_force=low), census(), [], TARGET,
                           mode='shadow', now=NOW)
        self.assertEqual(rec['would_set']['maxWorkers'], 300)
        self.assertEqual(rec['would_set']['nQueueLimitWorkerMax'], 150)

    def test_no_target_holds(self):
        state, reason, _ = decide('Q', reading(), census(), [], {'target_running': 0},
                                  mode='shadow', now=NOW)
        self.assertEqual((state, reason), ('held', 'no_target'))

    def test_stale_report_holds(self):
        state, reason, _ = decide('Q', reading(age=3600), census(), [], TARGET,
                                  mode='shadow', now=NOW)
        self.assertEqual((state, reason), ('held', 'report_stale'))

    def test_no_reading_holds(self):
        state, reason, _ = decide('Q', None, census(), [], TARGET, mode='shadow', now=NOW)
        self.assertEqual((state, reason), ('held', 'no_report'))

    def test_gate_red_drops_to_floor(self):
        state, reason, rec = decide('Q', reading(), census(), ['breaker open'], TARGET,
                                    mode='shadow', now=NOW)
        self.assertEqual((state, reason), ('would_set', 'degraded'))
        self.assertEqual(rec['would_set']['maxWorkers'], 50)

    def test_failing_pilots_drop_to_floor(self):
        _, reason, rec = decide('Q', reading(ended=40, unfinished=30), census(), [], TARGET,
                                mode='shadow', now=NOW)
        self.assertEqual(reason, 'degraded')
        self.assertIn('30 of 40', rec['gate_reason'])

    def test_no_work_never_raises(self):
        low = {'maxWorkers': 200, 'nQueueLimitWorkerMax': 100, 'nQueueLimitJobMax': 100}
        _, reason, rec = decide('Q', reading(in_force=low), census(activated=10), [], TARGET,
                                mode='shadow', now=NOW)
        self.assertEqual(reason, 'no_work')
        self.assertEqual(rec['would_set']['maxWorkers'], 200)

    def test_site_not_starting_after_latency(self):
        # Queue at Q, running below T, saturated since 2 h > p90 1 h.
        last = {'saturated': True, 'saturated_since': (NOW - timedelta(hours=2)).isoformat()}
        _, reason, rec = decide('Q', reading(queued=1200), census(), [], TARGET,
                                mode='shadow', now=NOW, last=last)
        self.assertEqual(reason, 'site_not_starting')
        _, reason, _ = decide('Q', reading(queued=1200), census(), [], TARGET,
                              mode='shadow', now=NOW)
        self.assertEqual(reason, 'regulate')

    def test_reading_from_report(self):
        report = {'age_seconds': 100, 'record': {
            'interval': {'seconds': 300},
            'harvester_db': {
                'launch_limits': [{'queue': 'Q', 'max_workers': '5000',
                                   'n_queue_limit_worker': '5000', 'n_queue_limit_job': '2000'}],
                'workers_now': {'Q': {'running': 540, 'submitted': 1295}},
                'workers_ended': {'sites': {'Q': {'ended': 202,
                                                  'by_status': {'finished': 200, 'failed': 2}}}}}}}
        r = harvester_reading('Q', {'pandaharvester01': report})
        self.assertEqual(r['in_force'], IN_FORCE)
        self.assertEqual((r['running'], r['queued'], r['ended_unfinished']), (540, 1295, 2))
        self.assertIsNone(harvester_reading('other', {'pandaharvester01': report}))


if __name__ == '__main__':
    unittest.main()
