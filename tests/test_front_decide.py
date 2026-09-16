"""The pressure front's decision, over a fake census and backlog
(swf_epicprod.front.decide is pure)."""
import unittest

from swf_epicprod.front import decide, fast_detectors


def census(not_started=0, running=0, ceiling=1000, median_h=2.0, hours=None,
           tasks_active=0, p90_start_h=1.0, gate=None, ungenerated=0):
    if hours is None and ceiling:
        hours = round(not_started * median_h / ceiling, 2)
    return {'not_started': not_started, 'running': running, 'ceiling': ceiling,
            'tasks_active': tasks_active, 'ungenerated': ungenerated,
            'hours_at_capacity': hours,
            'calibration': {'median_walltime_h': median_h,
                            'p90_start_latency_h': p90_start_h},
            'gate': gate or {'finished': 0, 'failed': 0, 'fast_failed': 0}}


def entry(name, rows=1000, level=1, problems=()):
    return {'task': name, 'pk': 1, 'level': level, 'level_source': 'task',
            'rows': rows, 'problems': list(problems), 'created_at': '2026-09-14T00:00:00'}


SETTINGS = {'feed': True, 'h_low': 8.0, 'h_high': 24.0, 'j_max': 0,
            't_max': 3, 'breaker': 'closed'}
GREEN = {'canary': {'status': 'healthy', 'age_h': 0.5, 'red': False, 'reason': ''},
         'credential': {'outcome': 'ok', 'age_h': 5.0, 'red': False, 'reason': ''}}


def run(census_q, backlog, settings=SETTINGS, gates=GREEN, feeds=(), *,
        enabled=True, mode='shadow', max_per_cycle=2, last_feed_age_h=None,
        jedi_throttled=False):
    return decide('Q', census_q, list(backlog), dict(settings), gates, list(feeds),
                  enabled=enabled, mode=mode, max_per_cycle=max_per_cycle,
                  last_feed_age_h=last_feed_age_h, jedi_throttled=jedi_throttled)


class DecideTest(unittest.TestCase):
    def test_switches_hold_before_anything(self):
        self.assertEqual(run(census(), [entry('t')], enabled=False)[0][:2], ('held', 'front_off'))
        off = dict(SETTINGS, feed=False)
        self.assertEqual(run(census(), [entry('t')], settings=off)[0][:2], ('held', 'queue_off'))

    def test_gates(self):
        red = {'canary': dict(GREEN['canary'], red=True, reason='canary failing'),
               'credential': GREEN['credential']}
        self.assertEqual(run(census(), [entry('t')], gates=red)[0][:2], ('degraded', 'canary'))
        burn = census(gate={'finished': 0, 'failed': 20, 'fast_failed': 15})
        self.assertEqual(run(burn, [entry('t')])[0][:2], ('degraded', 'burn_through'))
        storm = census(gate={'finished': 30, 'failed': 70, 'fast_failed': 0})
        self.assertEqual(run(storm, [entry('t')])[0][:2], ('degraded', 'failure_window'))

    def test_declared_downtime_holds_without_a_fault(self):
        # A rule in force, or a window inside the horizon, holds the
        # queue ahead of the measured gates: a hold, not degraded, so
        # the breaker and recovery never engage; the reason is the line.
        declared = dict(GREEN, declared={'red': True, 'state': 'in_force',
                                         'reason': 'offline until 09/15 00:21 UTC: scheduled downtime (xzhao@bnl.gov)'})
        out = run(census(not_started=5000), [entry('t')], gates=declared)
        self.assertEqual(out[0][:2], ('held', 'declared'))
        self.assertEqual(out[0][2]['gate_reason'], declared['declared']['reason'])
        self.assertEqual(out[0][2]['declared'], 'in_force')
        # Red canary and a declaration: the declaration is the reading.
        both = dict(declared, canary=dict(GREEN['canary'], red=True, reason='canary failing'))
        self.assertEqual(run(census(), [entry('t')], gates=both)[0][:2], ('held', 'declared'))
        # Nothing declared: the gates decide as before.
        none = dict(GREEN, declared={'red': False, 'state': '', 'reason': ''})
        self.assertEqual(run(census(not_started=5000), [entry('t')], gates=none)[0][:2],
                         ('supplied', 'supplied'))

    def test_supplied_and_no_work(self):
        # 5000 not started at 2 h over 1000 running slots: 10 h, above the low water mark.
        self.assertEqual(run(census(not_started=5000), [entry('t')])[0][:2], ('supplied', 'supplied'))
        self.assertEqual(run(census(), [])[0][:2], ('no_work', 'no_eligible_task'))
        self.assertEqual(run(census(), [entry('t', problems=['No priority'])])[0][:2],
                         ('no_work', 'no_eligible_task'))

    def test_feeds_in_priority_order_up_to_the_cap(self):
        backlog = [entry('a', rows=1000, level=1), entry('b', rows=1000, level=2),
                   entry('c', rows=1000, level=3)]
        out = run(census(), backlog)
        self.assertEqual([o[0] for o in out], ['would_feed', 'would_feed'])
        self.assertEqual([o[2]['candidate'] for o in out], ['a', 'b'])
        self.assertEqual(out[1][2]['committed_after'], 4.0)
        self.assertEqual(run(census(), backlog, mode='active')[0][0], 'fed')

    def test_feeding_stops_at_the_low_water_mark(self):
        # One 4000-row task is 8 h: the second candidate is not fed.
        out = run(census(), [entry('a', rows=4000), entry('b', rows=4000)])
        self.assertEqual([o[2]['candidate'] for o in out], ['a'])

    def test_never_past_the_high_water_mark(self):
        # 3500 pending is 7 h; a 9000-row task adds 18 h, past 24.
        out = run(census(not_started=3500), [entry('big', rows=9000)])
        self.assertEqual(out[0][:2], ('held', 'would_exceed_high'))
        self.assertEqual(out[0][2]['committed_after'], 25.0)
        # 13000 rows alone is 26 h: oversize, the operator's decision.
        self.assertEqual(run(census(), [entry('huge', rows=13000)])[0][:2], ('oversize', 'oversize_task'))

    def test_caps(self):
        self.assertEqual(run(census(tasks_active=3), [entry('t')])[0][:2], ('held', 'task_cap'))
        capped = dict(SETTINGS, j_max=1500)
        self.assertEqual(run(census(not_started=1000), [entry('t', rows=1000)], settings=capped)[0][:2],
                         ('held', 'job_cap'))

    def test_unsized_candidate_holds(self):
        self.assertEqual(run(census(), [entry('t', rows=None)])[0][:2], ('held', 'unsized_task'))

    def test_active_mode_feeds_and_names_the_task(self):
        out = run(census(), [entry('t')], mode='active')
        self.assertEqual(out[0][:2], ('fed', 'fed:t'))
        self.assertEqual(out[0][2]['candidate_pk'], 1)
        self.assertEqual(out[0][2]['phase'], 'unthrottled')

    def test_throttled_phase_counts_ungenerated_rows_and_retires_the_caps(self):
        # 5000 ungenerated rows are 10 h at capacity: unthrottled they are
        # invisible (JEDI activated everything it generated, the rest is
        # not yet committed); throttled they are the queue's committed depth.
        q = census(ungenerated=5000)
        self.assertEqual(run(q, [entry('t')])[0][0], 'would_feed')
        out = run(q, [entry('t')], jedi_throttled=True)
        self.assertEqual(out[0][:2], ('supplied', 'supplied'))
        self.assertEqual(out[0][2]['committed_h'], 10.0)
        self.assertEqual(out[0][2]['ungenerated_h'], 10.0)
        self.assertEqual(out[0][2]['phase'], 'throttled')
        # The job cap and the oversize hold retire: the pool is JEDI's to bound.
        capped = dict(SETTINGS, j_max=1500)
        self.assertEqual(run(census(not_started=1000), [entry('t', rows=1000)], settings=capped,
                             jedi_throttled=True)[0][0], 'would_feed')
        self.assertEqual(run(census(), [entry('huge', rows=13000)], jedi_throttled=True)[0][:2],
                         ('held', 'would_exceed_high'))

    def test_awaiting_observation_counts_the_feed(self):
        feeds = [{'candidate_rows': 2000}]
        out = run(census(), [entry('t')], feeds=feeds)
        self.assertEqual(out[0][:2], ('awaiting_observation', 'awaiting_observation'))
        self.assertEqual(out[0][2]['committed_h'], 4.0)

    def test_idle_capacity_needs_the_start_latency_to_pass(self):
        idle = census(not_started=1000, running=10, p90_start_h=2.0)
        self.assertEqual(run(idle, [entry('t')], last_feed_age_h=1.0)[0][0], 'would_feed')
        self.assertEqual(run(idle, [entry('t')], last_feed_age_h=3.0)[0][:2], ('idle_capacity', 'not_pulling'))
        self.assertEqual(run(idle, [entry('t')], last_feed_age_h=None)[0][0], 'would_feed')

    def test_no_calibration_holds(self):
        bare = {'not_started': 0, 'running': 0, 'ceiling': 0, 'hours_at_capacity': None,
                'calibration': {}, 'gate': {}}
        self.assertEqual(run(bare, [entry('t')])[0][:2], ('held', 'no_calibration'))

    def test_fast_detectors_need_a_minimum_sample(self):
        self.assertFalse(fast_detectors({'finished': 0, 'failed': 5, 'fast_failed': 5})['red'])
        self.assertFalse(fast_detectors({'finished': 10, 'failed': 20, 'fast_failed': 20})['red'])



class HoursSummaryTest(unittest.TestCase):
    def test_ready_job_hours_and_capacity_per_hour(self):
        from swf_epicprod.front import hours_summary
        backlog = {'Q': [entry('a', rows=1000, level=1), entry('b', rows=500, level=2),
                         entry('c', rows=500, level=None), entry('d', rows=None)]}
        latest = {'Q': {'median_walltime_h': 2.0, 'ceiling': 1000},
                  'R': {'median_walltime_h': 1.0, 'ceiling': 5}}
        out = hours_summary(backlog, latest)
        self.assertEqual(out['ready']['by_priority'], {'1': 2000.0, '2': 1000.0, '3': 0.0, 'unset': 1000.0})
        self.assertEqual(out['ready']['by_queue'], {'Q': 4000.0})
        self.assertEqual(out['ready']['total'], 4000.0)
        self.assertEqual(out['ready']['unsized_tasks'], 1)
        # Capacity is the ceiling: Q supplies 1000 job-hours per hour, R five.
        self.assertEqual(out['capacity'], {'by_queue': {'Q': 1000, 'R': 5}, 'total': 1005})
        # 4000 job-hours over 1005 per hour: about four hours of clock to drain.
        self.assertEqual(out['drain_h'], 3.98)


if __name__ == '__main__':
    unittest.main()
