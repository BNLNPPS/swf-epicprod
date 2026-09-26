"""The node harness's unit cutting (payload es/es_harness.py): the pool
over a feed, blocks of K single-event ranges cut when whole, partial
blocks at the end, one range per unit without K.

Plain unittest over the payload files; no pilot, no container.
"""
import json
import os
import random
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'swf_epicprod', 'payload', 'es'))

import es_harness as h  # noqa: E402


def ranges(events, lfn='f'):
    return [{'eventRangeID': f'r-{e}', 'startEvent': e, 'lastEvent': e, 'LFN': lfn} for e in events]


class TestLoopMode(unittest.TestCase):
    """TEST AND DEMO ONLY: replay filler after the file's ranges are all in."""

    def test_pool_stops_asking_at_expected_so_no_more_events_is_never_drawn(self):
        feed = h.FileFeed.__new__(h.FileFeed)
        feed.ranges, feed.exhausted, feed.reports = ranges(range(1, 11)), False, []
        pool = h.Pool(feed, expected=10)
        time.sleep(0.3)
        self.assertEqual(len(pool), 10)
        self.assertTrue(pool.complete)
        self.assertFalse(feed.exhausted)           # the eleventh ask never happened

    def test_replay_cycles_the_events_and_is_marked(self):
        state = {'pass': 1, 'next': 1}
        tmpl = ranges([1])[0]
        u1 = h.replay_unit(tmpl, state, 4, 6)
        u2 = h.replay_unit(tmpl, state, 4, 6)
        u3 = h.replay_unit(tmpl, state, 4, 6)
        self.assertEqual([r['startEvent'] for r in u1], [1, 2, 3, 4])
        self.assertEqual([r['startEvent'] for r in u2], [5, 6])
        self.assertEqual([r['startEvent'] for r in u3], [1, 2, 3, 4])
        self.assertTrue(all(r['replay'] for r in u1 + u2 + u3))
        self.assertEqual(u3[0]['eventRangeID'], 'replay2-1')


class TestStreamingStart(unittest.TestCase):
    def test_free_slot_takes_a_partial_block_of_min_events(self):
        """A free slot starts on the contiguous run it has (at least min_events)."""
        feed = h.FileFeed.__new__(h.FileFeed)
        feed.ranges, feed.exhausted, feed.reports = ranges(range(1, 21)), False, []
        pool = h.Pool(feed, lookahead=20)
        time.sleep(0.3)
        feed.exhausted = False                   # more of the block is still "coming"
        unit = pool.take_unit(163, min_events=16)
        self.assertEqual([r['startEvent'] for r in unit][:2], [1, 2])
        self.assertGreaterEqual(len(unit), 16)
        self.assertEqual(pool.take_unit(163, min_events=0), [])   # without a minimum it waits for the block

    def test_the_short_rest_of_a_cut_block_does_not_block_the_pool(self):
        """Job 3618888: a streaming start took events 251-496 of block 251-500,
        and the 4-event rest held every slot idle until the file ran out."""
        feed = h.FileFeed.__new__(h.FileFeed)
        feed.ranges, feed.exhausted, feed.reports = ranges(range(1, 21)), False, []
        pool = h.Pool(feed, lookahead=20)
        time.sleep(0.3)
        feed.exhausted = False                   # more of the file is still "coming"
        with pool.lock:
            del pool.ranges[6:]                  # events 1-6 in, 7-10 not yet
        self.assertEqual([r['startEvent'] for r in pool.take_unit(10, min_events=4)], list(range(1, 7)))
        with pool.lock:
            pool.ranges = ranges(range(7, 21))
        self.assertEqual([r['startEvent'] for r in pool.take_unit(10, min_events=16)], [7, 8, 9, 10])
        self.assertEqual([r['startEvent'] for r in pool.take_unit(10, min_events=16)], list(range(11, 21)))


class TestNoHeadOfLine(unittest.TestCase):
    def _pool(self, events):
        feed = h.FileFeed.__new__(h.FileFeed)
        feed.ranges, feed.exhausted, feed.reports = ranges(events), False, []
        pool = h.Pool(feed, lookahead=len(events))
        time.sleep(0.3)
        feed.exhausted = False                   # more is still "coming"
        return pool

    def test_a_late_event_is_cut_when_the_rest_of_its_block_is_gone(self):
        """Job 3618931: event 5984 arrived after 5751-5983 and 5985-6000 were cut."""
        pool = self._pool([1, 2, 3, 4, 6, 7, 8, 9, 10])
        self.assertEqual([r['startEvent'] for r in pool.take_unit(10, min_events=3)], [1, 2, 3, 4])
        self.assertEqual([r['startEvent'] for r in pool.take_unit(10, min_events=3)], [6, 7, 8, 9, 10])
        with pool.lock:
            pool.ranges = ranges([5])
        self.assertEqual([r['startEvent'] for r in pool.take_unit(10, min_events=3)], [5])

    def test_a_run_that_must_wait_does_not_hold_up_the_runs_behind_it(self):
        pool = self._pool([1, 2] + list(range(11, 21)))
        self.assertEqual([r['startEvent'] for r in pool.take_unit(10, min_events=16)], list(range(11, 21)))
        self.assertEqual(pool.take_unit(10, min_events=16), [])      # 1-2 still wait for 3-10


class TestDrain(unittest.TestCase):
    def test_a_capped_unit_is_cut_at_once_and_its_rest_follows(self):
        feed = h.FileFeed.__new__(h.FileFeed)
        feed.ranges, feed.exhausted, feed.reports = ranges(range(1, 21)), False, []
        pool = h.Pool(feed, lookahead=20)
        time.sleep(0.3)
        feed.exhausted = False
        self.assertEqual([r['startEvent'] for r in pool.take_unit(10, min_events=16, max_events=3)], [1, 2, 3])
        self.assertEqual([r['startEvent'] for r in pool.take_unit(10, min_events=16)], list(range(4, 11)))

    def test_seconds_per_event_over_the_finished_units(self):
        self.assertIsNone(h.seconds_per_event([]))
        self.assertAlmostEqual(h.seconds_per_event([(16, 118.0), (250, 540.0)]), 658.0 / 266)


class TestLookahead(unittest.TestCase):
    def test_pool_stops_at_the_lookahead_until_wanted(self):
        """The pool holds at most the lookahead, so "No more events" is not
        drawn from the pilot at the start (job 3618786)."""
        feed = h.FileFeed.__new__(h.FileFeed)
        feed.ranges, feed.exhausted, feed.reports = ranges(range(1, 41)), False, []
        pool = h.Pool(feed, lookahead=10)
        time.sleep(0.3)
        self.assertEqual(len(pool), 10)
        self.assertFalse(feed.exhausted)
        self.assertEqual(len(pool.take_unit(5)), 5)
        time.sleep(0.3)
        self.assertEqual(len(pool), 10)          # refilled to the lookahead, no further
        pool.want()
        time.sleep(0.3)
        self.assertEqual(len(pool), 11)          # one more on demand
        taken = 16                               # 5 cut above, 11 pooled
        deadline = time.time() + 10
        while not pool.exhausted and time.time() < deadline:
            taken += len(pool.take_unit(5) or pool.want() or [])
            time.sleep(0.05)
        self.assertTrue(pool.exhausted)          # "No more events" only once all 40 were asked for


class SlowFeed(h.FileFeed):
    """A feed that hands one range per ask with a pause, as the pilot does."""

    def __init__(self, rs, pause=0.01):
        path = tempfile.mktemp()
        with open(path, 'w') as f:
            json.dump(rs, f)
        super().__init__(path)
        self.pause = pause

    def ask(self):
        time.sleep(self.pause)
        return super().ask()


def cut_all(pool, K, wait=2.0):
    """Every unit the pool yields until it is drained."""
    units, t0 = [], time.time()
    while time.time() - t0 < wait:
        m = pool.take_unit(K)
        if m:
            spec = h.unit_spec(m, K, 'p', 'x', 's')
            units.append((m[0]['startEvent'], m[-1]['lastEvent'], spec.get('block')))
        elif pool.exhausted and not len(pool):
            break
        else:
            time.sleep(0.01)
    return units


class PoolTests(unittest.TestCase):
    def test_blocks_cut_whole_from_a_shuffled_feed(self):
        ev = list(range(1, 21))
        random.seed(1)
        random.shuffle(ev)
        pool = h.Pool(SlowFeed(ranges(ev)))
        self.assertEqual(sorted(cut_all(pool, 5)), [(1, 5, 0), (6, 10, 1), (11, 15, 2), (16, 20, 3)])

    def test_a_block_waits_until_it_is_whole(self):
        # events 1..4 present, 5 still coming: block 0 is not cut yet
        feed = SlowFeed(ranges([1, 2, 3, 4, 5, 6, 7, 8, 9, 10]), pause=0.2)
        pool = h.Pool(feed)
        time.sleep(0.95)                        # about four ranges in
        self.assertEqual(pool.take_unit(5), [])
        self.assertEqual(cut_all(pool, 5, wait=5), [(1, 5, 0), (6, 10, 1)])

    def test_a_block_missing_its_first_event_waits(self):
        # 2..5 in, 1 still coming: no unit until 1 arrives
        feed = SlowFeed(ranges([2, 3, 4, 5, 1, 6, 7, 8, 9, 10]), pause=0.15)
        pool = h.Pool(feed)
        time.sleep(0.7)                         # 2..5 in
        self.assertEqual(pool.take_unit(5), [])
        self.assertEqual(cut_all(pool, 5, wait=5), [(1, 5, 0), (6, 10, 1)])

    def test_partial_last_block_and_holes_cut_at_the_end(self):
        pool = h.Pool(SlowFeed(ranges([1, 2, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18])))
        self.assertEqual(sorted(cut_all(pool, 5)), [(1, 2, 0), (5, 5, 0), (6, 10, 1), (11, 15, 2), (16, 18, 3)])

    def test_one_range_per_unit_without_k(self):
        pool = h.Pool(SlowFeed([{'eventRangeID': 'b', 'startEvent': 6, 'lastEvent': 10, 'LFN': 'f'},
                                {'eventRangeID': 'a', 'startEvent': 1, 'lastEvent': 5, 'LFN': 'f'}]))
        units = cut_all(pool, 0)
        # a range is its own unit, taken as it arrives
        self.assertEqual(sorted(u[:2] for u in units), [(1, 5), (6, 10)])
        self.assertTrue(all(u[2] is None for u in units))

    def test_lfn_change_breaks_a_unit(self):
        pool = h.Pool(SlowFeed(ranges([1, 2]) + ranges([3, 4], lfn='g')))
        self.assertEqual([u[:2] for u in cut_all(pool, 5)], [(1, 2), (3, 4)])

    def test_unit_spec_names_the_block_and_carries_the_members(self):
        m = ranges([6, 7, 8, 9, 10])
        spec = h.unit_spec(m, 5, 'row/path', 'hepmc3.tree.root', 'stamp')
        self.assertEqual(spec['unit_id'], 'r-6')
        self.assertEqual((spec['range']['startEvent'], spec['range']['lastEvent']), (6, 10))
        self.assertEqual(spec['block'], 1)
        self.assertEqual(spec['unit_events'], 5)
        self.assertEqual(len(spec['ranges']), 5)


if __name__ == '__main__':
    unittest.main()


class TestRecordOut(unittest.TestCase):
    """The record shipped off the node as it is made (es_record.py)."""

    def test_writes_the_snapshot_under_the_job_key_and_halts_for_good(self):
        import es_record as r
        sent = []
        orig = r.put_object
        r.put_object = lambda body, bucket, key, *a, **k: (sent.append((key, json.loads(body))), (True, ''))[1]
        env = {'PANDAID': '42', 'REPORT_OUT_BUCKET': 'b', 'REPORT_OUT_ACCESS_KEY_ID': 'k',
               'REPORT_OUT_SECRET_ACCESS_KEY': 's'}
        old = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        try:
            rec = r.RecordOut(lambda: {'done': [r.trim_unit({'unit_id': 'u', 'events': 3, 'handoff': {'x': 1},
                                                             'range': {'startEvent': 1, 'lastEvent': 3, 'LFN': 'f'}})]},
                              interval=0.05).start()
            time.sleep(0.2)
            rec.halt()
            time.sleep(0.1)
            n = len(sent)
            self.assertGreaterEqual(n, 1)
            key, body = sent[-1]
            self.assertEqual(key, 'reports/42/es.json')
            self.assertEqual(body['done'][0], {'unit_id': 'u', 'events': 3, 'range': {'startEvent': 1, 'lastEvent': 3}})
            self.assertIn('written_at', body)
            rec.nudge()
            time.sleep(0.1)
            self.assertEqual(len(sent), n)            # halted: nothing more leaves the node
        finally:
            r.put_object = orig
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
