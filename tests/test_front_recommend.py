"""The placement recommender (swf_epicprod.front.recommend and its parts),
pure over the front's stored state (CONTINUOUS_PRODUCTION.md, Placement)."""
import unittest

from swf_epicprod.front import fit_problems, gate_blockers, recommend, stall_signal

GREEN = {'canary': {'status': 'healthy', 'red': False},
         'credential': {'red': False}, 'declared': {'red': False},
         'fast': {'red': False}, 'breaker': 'closed'}
LIMITS = {'maxtime_h': 6.0, 'maxrss_mb': 48000, 'corecount': 8, 'status': 'online'}
NEED = {'walltime_h': 2.0, 'cores': 1, 'ram_mb_per_core': 4096,
        'memory_mb': 4096, 'rows': 100}


def _decision(committed_h=0.0, not_started=0, p90=10.0, median=0.2, ceiling=1000):
    return {'committed_h': committed_h, 'h_low': 8.0, 'h_high': 24.0,
            'not_started': not_started, 'running': 0, 'ceiling': ceiling,
            'median_walltime_h': median, 'p90_start_latency_h': p90,
            'breaker': 'closed', 'state': 'held', 'reason': 'queue_off'}


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
                          'last_start_h': 2.0}, 15.0)
        self.assertEqual(s['state'], 'slow')

    def test_within_bar_is_quiet(self):
        s = stall_signal({'queued': 50, 'running': 0, 'oldest_wait_h': 10.0,
                          'last_start_h': None}, 15.0)
        self.assertEqual(s['state'], '')

    def test_uncalibrated_default_bar(self):
        s = stall_signal({'queued': 5, 'running': 0, 'oldest_wait_h': 7.0,
                          'last_start_h': None}, None)
        self.assertEqual((s['state'], s['bar_h']), ('stalled', 6.0))

    def test_nothing_waiting(self):
        self.assertEqual(stall_signal({'queued': 0, 'running': 4}, 15.0)['state'], '')


if __name__ == '__main__':
    unittest.main()
