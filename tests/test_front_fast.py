"""The front's fast detectors (CONTINUOUS_PRODUCTION.md, The tripwire):
the failure window over the failures the census counts against the queue,
and an operator's waiver. Pure."""
import unittest

from swf_epicprod.front import fast_detectors

WAIVER = {'until': '2026-10-21T06:00:00+00:00', 'reason': 'eAu memory kills, f-15', 'by': 'wenaus'}


class FastDetectorTest(unittest.TestCase):
    def test_failure_window_red(self):
        r = fast_detectors({'finished': 3, 'failed': 260, 'fast_failed': 4})
        self.assertTrue(r['red'])
        self.assertEqual(r['code'], 'failure_window')

    def test_explained_failures_are_not_counted(self):
        # The census counts a finding's tasks apart; only the rest decide.
        r = fast_detectors({'finished': 3, 'failed': 12, 'fast_failed': 0,
                            'explained': 1097, 'explained_by': ['f-15']})
        self.assertFalse(r['red'])
        self.assertEqual(r['explained'], 1097)
        self.assertEqual(r['explained_by'], ['f-15'])

    def test_waiver_turns_red_green_and_says_so(self):
        r = fast_detectors({'finished': 0, 'failed': 1109, 'fast_failed': 0}, WAIVER)
        self.assertFalse(r['red'])
        self.assertEqual(r['code'], 'waived')
        self.assertEqual(r['waived'], 'failure_window')
        self.assertIn('by wenaus', r['reason'])
        self.assertIn('failure window: 1109 of 1109', r['reason'])

    def test_waiver_on_a_green_queue_changes_nothing(self):
        r = fast_detectors({'finished': 300, 'failed': 2, 'fast_failed': 0}, WAIVER)
        self.assertFalse(r['red'])
        self.assertEqual(r['code'], '')

    def test_burn_through_waived(self):
        r = fast_detectors({'finished': 0, 'failed': 20, 'fast_failed': 20}, WAIVER)
        self.assertFalse(r['red'])
        self.assertEqual(r['waived'], 'burn_through')


if __name__ == '__main__':
    unittest.main()
