"""The placement recommender (swf_epicprod.front.recommend and its parts),
pure over the front's stored state (CONTINUOUS_PRODUCTION.md, Placement)."""
import unittest
from unittest.mock import patch

_closed = patch('swf_epicprod.front.closed_queues', return_value={})


def setUpModule():
    _closed.start()


def tearDownModule():
    _closed.stop()

from swf_epicprod.front import (core_limited, core_limits, draw_order, fit_problems,
                                 gate_blockers, left_in_pool, open_to, recommend, stall_signal)

GREEN = {'canary': {'status': 'healthy', 'red': False},
         'credential': {'red': False}, 'declared': {'red': False},
         'fast': {'red': False}, 'breaker': 'closed'}
LIMITS = {'maxtime_h': 6.0, 'maxrss_mb': 48000, 'corecount': 8, 'status': 'online'}
NEED = {'walltime_h': 2.0, 'cores': 1, 'ram_mb_per_core': 4096,
        'memory_mb': 4096, 'rows': 100}


def _decision(committed_h=0.0, not_started=0, p90=10.0, median=0.2, ceiling=1000,
              feed_switch=True):
    return {'committed_h': committed_h, 'h_low': 8.0, 'h_high': 24.0,
            'not_started': not_started, 'running': 0, 'ceiling': ceiling,
            'median_walltime_h': median, 'p90_start_latency_h': p90,
            'breaker': 'closed', 'state': 'held', 'reason': 'queue_off',
            'feed_switch': feed_switch}


def _state(**queues):
    return {'queues': {q: d for q, (d, _, _) in queues.items()},
            'placement': {q: {'gates': g, 'limits': lim}
                          for q, (_, g, lim) in queues.items()}}


class FitTest(unittest.TestCase):
    def test_walltime_memory_cores(self):
        need = dict(NEED, walltime_h=8.0, cores=16, memory_mb=65536)
        problems = fit_problems(need, LIMITS)
        self.assertEqual(len(problems), 3)

    def test_no_limit_declared_fits(self):
        self.assertEqual(fit_problems(NEED, {'maxtime_h': None, 'maxrss_mb': None,
                                             'corecount': None}), [])


class GateTest(unittest.TestCase):
    def test_red_gates_and_breaker(self):
        gates = dict(GREEN, canary={'status': 'failing', 'red': True,
                                    'reason': 'canary failing'})
        self.assertEqual(gate_blockers(gates, {'breaker': 'open'}),
                         ['breaker open', 'canary failing'])

    def test_unrecorded(self):
        self.assertEqual(len(gate_blockers(None)), 1)


class RecommendTest(unittest.TestCase):
    def test_least_depth_wins_among_healthy(self):
        state = _state(A=(_decision(committed_h=5.0), GREEN, LIMITS),
                       B=(_decision(committed_h=1.0), GREEN, LIMITS))
        r = recommend(state, NEED, ['A', 'B'])
        self.assertEqual(r['recommended'], 'B')
        self.assertIn('1.0 h of queued work', r['reason'])

    def test_the_queue_short_the_most_slot_hours_wins(self):
        # 26 October: depth in hours alone put GREX (949 slots) ahead of OSG
        # (4,425 slots) while OSG ran out of work.
        state = _state(OSG=(_decision(committed_h=2.0, ceiling=4425), GREEN, LIMITS),
                       GREX=(_decision(committed_h=0.5, ceiling=949), GREEN, LIMITS))
        r = recommend(state, NEED, ['OSG', 'GREX'])
        self.assertEqual(r['recommended'], 'OSG')
        self.assertIn('26,550 slot-hours short of its low mark', r['reason'])

    def test_a_failing_queue_ranks_after_a_clean_one(self):
        failing = dict(_decision(committed_h=0.0, ceiling=4000), gate_finished=4597, gate_failed=2845)
        clean = dict(_decision(committed_h=4.0, ceiling=1000), gate_finished=13062, gate_failed=69)
        state = _state(GREX=(failing, GREEN, LIMITS), OSG=(clean, GREEN, LIMITS))
        r = recommend(state, NEED, ['GREX', 'OSG'])
        self.assertEqual(r['recommended'], 'OSG')
        self.assertIn('0.5% of its recent outcomes failed', r['reason'])
        rows = {row['queue']: row for row in r['rows']}
        self.assertTrue(rows['GREX']['failing'])
        self.assertEqual(rows['GREX']['failed_share'], 0.382)

    def test_canary_before_depth(self):
        degraded = dict(GREEN, canary={'status': 'degraded', 'red': False})
        state = _state(A=(_decision(committed_h=0.0), degraded, LIMITS),
                       B=(_decision(committed_h=4.0), GREEN, LIMITS))
        self.assertEqual(recommend(state, NEED, ['A', 'B'])['recommended'], 'B')

    def test_gated_and_unfit_excluded(self):
        red = dict(GREEN, declared={'red': True, 'reason': 'downtime'})
        short = dict(LIMITS, maxtime_h=1.0)
        state = _state(A=(_decision(), red, LIMITS), B=(_decision(), GREEN, short),
                       C=(_decision(committed_h=20.0), GREEN, LIMITS))
        r = recommend(state, NEED, ['A', 'B', 'C'])
        self.assertEqual(r['recommended'], 'C')
        rows = {row['queue']: row for row in r['rows']}
        self.assertEqual(rows['A']['blockers'], ['downtime'])
        self.assertTrue(rows['B']['fit'])

    def test_feed_off_not_recommended_but_marked(self):
        # The Site control lets a person choose a queue whose only blocker
        # is its feed switch, so the row says which blocker that is.
        state = _state(A=(_decision(committed_h=0.0, feed_switch=False), GREEN, LIMITS),
                       B=(_decision(committed_h=5.0), GREEN, LIMITS))
        r = recommend(state, NEED, ['A', 'B'])
        self.assertEqual(r['recommended'], 'B')
        rows = {row['queue']: row for row in r['rows']}
        self.assertEqual(rows['A']['blockers'], ['feed off'])
        self.assertTrue(rows['A']['feed_off'])
        self.assertFalse(rows['B']['feed_off'])

    def test_uncalibrated_empty_counts_as_empty_known_p90_breaks_tie(self):
        state = _state(A=(_decision(committed_h=None, p90=None), GREEN, LIMITS),
                       B=(_decision(committed_h=0.0, p90=15.0), GREEN, LIMITS),
                       C=(_decision(committed_h=None, not_started=3, p90=None), GREEN, LIMITS))
        r = recommend(state, NEED, ['A', 'B', 'C'])
        order = sorted((row for row in r['rows'] if row.get('rank')), key=lambda x: x['rank'])
        self.assertEqual([row['queue'] for row in order], ['B', 'A', 'C'])

    def test_task_hours_at_capacity(self):
        state = _state(A=(_decision(median=0.5, ceiling=100), GREEN, LIMITS))
        r = recommend(state, NEED, ['A'])
        self.assertEqual(r['rows'][0]['task_h'], 0.5)

    def test_no_state_says_so(self):
        r = recommend({}, NEED, ['A'])
        self.assertEqual(r['recommended'], '')
        self.assertIn('none is stored', r['reason'])


class StallTest(unittest.TestCase):
    def test_stalled_nothing_running_nothing_started(self):
        s = stall_signal({'queued': 120, 'running': 0, 'oldest_wait_h': 31.0,
                          'last_start_h': None}, 15.0)
        self.assertEqual(s['state'], 'stalled')
        self.assertIn('no job has started', s['reason'])

    def test_slow_when_some_run(self):
        s = stall_signal({'queued': 50, 'running': 3, 'oldest_wait_h': 20.0,
                          'last_start_h': 0.5}, 15.0)
        self.assertEqual(s['state'], 'slow')

    def test_recent_start_is_not_stalled(self):
        s = stall_signal({'queued': 50, 'running': 0, 'oldest_wait_h': 20.0,
                          'last_start_h': 0.1}, 15.0)
        self.assertEqual(s['state'], 'slow')

    def test_within_bar_is_quiet(self):
        s = stall_signal({'queued': 50, 'running': 0, 'oldest_wait_h': 0.1,
                          'last_start_h': None}, 15.0)
        self.assertEqual(s['state'], '')

    def test_uncalibrated_default_bar(self):
        s = stall_signal({'queued': 5, 'running': 0, 'oldest_wait_h': 7.0,
                          'last_start_h': None}, None)
        self.assertEqual((s['state'], s['bar_h']), ('stalled', 0.25))

    def test_floor_on_tiny_p90(self):
        s = stall_signal({'queued': 5, 'running': 0, 'oldest_wait_h': 0.5,
                          'last_start_h': None}, 0.04)
        self.assertEqual((s['state'], s['bar_h']), ('stalled', 0.25))

    def test_nothing_waiting(self):
        self.assertEqual(stall_signal({'queued': 0, 'running': 4}, 15.0)['state'], '')


def _entry(name, problems=(), level=1):
    return {'task': name, 'pk': 1, 'level': level, 'rows': 100, 'need': NEED,
            'problems': list(problems), 'created_at': '2026-09-30T00:00:00'}


class ReadyPoolTest(unittest.TestCase):
    """Ready tasks hold no queue (Torre Wenaus, 2026-10-28): a queue below
    its low mark draws on the pool, the neediest first."""

    def _state(self, a=6.0, b=1.0, **more):
        return _state(A=(dict(_decision(committed_h=a), **more), GREEN, LIMITS),
                      B=(_decision(committed_h=b), GREEN, LIMITS))

    def test_the_neediest_queue_draws_first(self):
        self.assertEqual(draw_order(['A', 'B', 'C'], ['A', 'B'], self._state()), ['B', 'A', 'C'])
        self.assertEqual(draw_order(['A', 'B'], [], self._state()), ['A', 'B'])

    def test_a_drawn_task_carries_its_placement_and_the_pool_stays_agnostic(self):
        pool = [_entry('t1')]
        drawn = open_to(pool, 'A', self._state())
        self.assertEqual(drawn[0]['placement']['recommended'], 'A')
        self.assertNotIn('placement', pool[0])
        self.assertEqual(open_to(pool, 'B', self._state())[0]['placement']['recommended'], 'B')

    def test_a_queue_with_its_feed_off_or_a_task_not_ready_draws_nothing(self):
        self.assertEqual(open_to([_entry('t1')], 'A', self._state(feed_switch=False)), [])
        self.assertEqual(open_to([_entry('t1', problems=['x'])], 'A', self._state()), [])

    def test_what_no_queue_took_says_why(self):
        pool = [_entry('t1', problems=['x']), _entry('t2'), _entry('t3')]
        open_to(pool, 'A', self._state())
        left_in_pool(pool, ['A'])
        self.assertEqual(pool[0]['unplaced_reason'], 'not ready')
        self.assertNotIn('unplaced_reason', pool[1])
        self.assertNotIn('open_to', pool[1])
        self.assertEqual(left_in_pool([_entry('t4')], [])[0]['unplaced_reason'],
                         'no queue the front feeds')


class CoreLimitTest(unittest.TestCase):
    def test_a_limited_queue_is_sized_at_its_limit(self):
        census = {'queues': {'G': {'ceiling': 949, 'hours_at_capacity': 1.0},
                             'O': {'ceiling': 4000, 'hours_at_capacity': 2.0}}}
        out = core_limited(census, {'G': 296, 'O': 5000})
        self.assertEqual((out['queues']['G']['ceiling'], out['queues']['G']['hours_at_capacity']),
                         (296, 3.21))
        self.assertEqual(out['queues']['O'], census['queues']['O'])
        self.assertEqual(census['queues']['G']['ceiling'], 949)

    def test_only_active_limits_count(self):
        cfg = {'pilot.queue.G.worker_limit': {'active': True, 'core_limit': 296},
               'pilot.queue.O.worker_limit': {'active': False, 'core_limit': 100},
               'pilot.queue.G.enabled': False}
        self.assertEqual(core_limits(cfg), {'G': 296})

if __name__ == '__main__':
    unittest.main()
