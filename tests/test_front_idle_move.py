"""The idle-queue move (swf_epicprod.idle_move.choose), pure over this
cycle's decisions and the candidate tasks (CONTINUOUS_PRODUCTION.md,
Idle-queue move)."""
import unittest

from swf_epicprod.idle_move import choose, idle_targets

OSG, GREX = 'BNL_OSG_PanDA_1', 'UM_GREX_PanDA_1'


def _decision(state, committed_h, median, ceiling, canary='healthy', feed=True):
    return {'state': state, 'committed_h': committed_h, 'h_low': 8.0, 'h_high': 24.0,
            'median_walltime_h': median, 'ceiling': ceiling, 'canary': canary,
            'feed_switch': feed}


def _task(tid, site=GREX, queued=0, ungenerated=500, started=0, priority=950,
          eligible=(OSG, GREX), recent=False):
    return {'jeditaskid': tid, 'taskname': f't{tid}', 'site': site, 'queued': queued,
            'ungenerated': ungenerated, 'started': started, 'priority': priority,
            'eligible': list(eligible), 'recent_move': recent, 'prod_task_pk': tid}


# 2026-10-26 07:00 ET: OSG idle with nothing ready; GREX 1.91 h deep.
DECISIONS = {OSG: _decision('no_work', 0.0, 0.325, 4425),
             GREX: _decision('no_work', 1.91, 0.277, 949)}


class IdleMoveTest(unittest.TestCase):

    def test_drain_case_moves_unstarted_tasks_to_the_idle_queue(self):
        tasks = [_task(t) for t in (41244, 41245, 41247, 41248)]
        moves, skipped = choose(DECISIONS, tasks, max_moves=4)
        self.assertEqual([m['to'] for m in moves], [OSG] * 4)
        self.assertEqual(skipped, [])
        # Newest first: the work that would start last at GREX.
        self.assertEqual(moves[0]['jeditaskid'], 41248)
        self.assertLess(moves[-1]['to_after_h'], moves[-1]['from_after_h'])

    def test_a_task_with_anything_started_stays(self):
        moves, _ = choose(DECISIONS, [_task(41235, queued=295, ungenerated=0, started=205)],
                          max_moves=4)
        self.assertEqual(moves, [])

    def test_tasks_without_jobs_go_before_tasks_with_queued_jobs(self):
        moves, _ = choose(DECISIONS, [_task(1, queued=200, ungenerated=0), _task(2)],
                          max_moves=1)
        self.assertEqual(moves[0]['jeditaskid'], 2)

    def test_moves_stop_when_the_queues_are_level(self):
        decisions = {OSG: _decision('no_work', 0.0, 0.3, 300),
                     GREX: _decision('no_work', 0.6, 0.3, 300)}
        # 500 rows are 0.5 h at either queue: 0.5 <= 0.6 - 0.5 fails.
        moves, skipped = choose(decisions, [_task(1)], max_moves=4)
        self.assertEqual(moves, [])
        self.assertIn('shallower', skipped[0]['why'])

    def test_target_must_be_idle_healthy_and_below_its_low_mark(self):
        for target in (_decision('supplied', 0.0, 0.325, 4425),
                       _decision('no_work', 9.0, 0.325, 4425),
                       _decision('no_work', 0.0, 0.325, 4425, canary='insufficient'),
                       _decision('no_work', 0.0, 0.325, 4425, feed=False)):
            decisions = dict(DECISIONS, **{OSG: target})
            self.assertEqual(idle_targets(decisions).get(OSG), None)
            self.assertEqual(choose(decisions, [_task(1)], max_moves=4)[0], [])

    def test_a_task_the_target_cannot_take_stays(self):
        moves, skipped = choose(DECISIONS, [_task(1, eligible=(GREX,))], max_moves=4)
        self.assertEqual(moves, [])
        self.assertTrue(skipped)

    def test_a_recently_moved_task_stays(self):
        moves, skipped = choose(DECISIONS, [_task(1, recent=True)], max_moves=4)
        self.assertEqual(moves, [])
        self.assertEqual(skipped[0]['why'], 'moved within the cooldown')

    def test_a_task_at_the_idle_queue_is_not_moved_off_it(self):
        moves, _ = choose(DECISIONS, [_task(1, site=OSG)], max_moves=4)
        self.assertEqual(moves, [])

    def test_max_moves_bounds_the_cycle(self):
        moves, _ = choose(DECISIONS, [_task(t) for t in range(10)], max_moves=2)
        self.assertEqual(len(moves), 2)


if __name__ == '__main__':
    unittest.main()
