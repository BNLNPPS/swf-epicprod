"""Call home (payload call_home.py): status sent only while the pilot's
debug-mode file exists, at once when it appears and every interval
after, paused when it goes, never past the per-job cap.

Plain unittest; the object write is replaced, nothing leaves the host.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'swf_epicprod', 'payload'))

import call_home as ch  # noqa: E402

ENV = {'PANDAID': '123', 'REPORT_OUT_BUCKET': 'b', 'REPORT_OUT_ACCESS_KEY_ID': 'k',
       'REPORT_OUT_SECRET_ACCESS_KEY': 's'}


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class TestCallHome(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.flag = os.path.join(self.dir, ch.DEBUG_MODE_FILE)
        self.clock = Clock()
        self.sent = []
        patcher = mock.patch.object(ch, 'send', side_effect=lambda body, key: self.sent.append(
            (key, json.loads(body))) or True)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, ENV)
        env.start()
        self.addCleanup(env.stop)
        self.home = ch.CallHome(self.flag, ch.status_key(), lambda: {'stage': 'npsim'},
                                interval=600, clock=self.clock)

    def debug(self, on):
        if on:
            with open(self.flag, 'w') as f:
                f.write('{"debug": true}')
        elif os.path.exists(self.flag):
            os.remove(self.flag)

    def test_nothing_without_the_file(self):
        self.assertFalse(self.home.tick())
        self.assertEqual(self.sent, [])

    def test_sends_at_once_then_every_interval(self):
        self.debug(True)
        self.assertTrue(self.home.tick())
        self.clock.t += 60
        self.assertFalse(self.home.tick())
        self.clock.t += 540
        self.assertTrue(self.home.tick())
        self.assertEqual([k for k, _ in self.sent], ['status/123.json'] * 2)
        self.assertEqual(self.sent[0][1]['stage'], 'npsim')
        self.assertEqual([s['sequence'] for _, s in self.sent], [0, 1])

    def test_off_pauses_and_on_again_sends_at_once(self):
        self.debug(True)
        self.home.tick()
        self.debug(False)
        self.clock.t += 600
        self.assertFalse(self.home.tick())
        self.clock.t += 10
        self.debug(True)
        self.assertTrue(self.home.tick())
        self.assertEqual(len(self.sent), 2)

    def test_cap(self):
        self.debug(True)
        for _ in range(ch.MAX_WRITES + 5):
            self.home.tick()
            self.clock.t += 600
        self.assertEqual(len(self.sent), ch.MAX_WRITES)

    def test_snapshot_error_is_sent_not_raised(self):
        self.home.snapshot = lambda: 1 / 0
        self.debug(True)
        self.assertTrue(self.home.tick())
        self.assertIn('ZeroDivisionError', self.sent[0][1]['snapshot_error'])

    def test_off_without_job_id_or_credential(self):
        with mock.patch.dict(os.environ, {'PANDAID': ''}):
            self.assertIsNone(ch.status_key())
        with mock.patch.dict(os.environ, {'REPORT_OUT_SECRET_ACCESS_KEY': ''}):
            self.assertFalse(ch.configured())
            home = ch.CallHome(self.flag, 'status/1.json', dict).start()
            self.assertFalse(home.thread.is_alive())


if __name__ == '__main__':
    unittest.main()
