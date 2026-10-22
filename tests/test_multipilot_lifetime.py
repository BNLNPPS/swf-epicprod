"""Exercise actual launcher children against allocation/queue-data mismatches."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / 'pilot' / 'epicprod-multipilot.sh'
spec = importlib.util.spec_from_file_location('grex_installer', ROOT / 'scripts' / 'configure-grex-lifetime.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class MultipilotLifetimeTests(unittest.TestCase):
    def run_launcher(self, *, maximum=43200, floor=510, allocation=43200,
                     reserve=10800, queuedata=True, extra=None):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory)
            data = {'maxtime': maximum, 'timefloor': floor, 'container_options': 'unchanged'}
            if queuedata:
                (p / 'queuedata.json').write_text(json.dumps(data, indent=4) + '\n')
            wrapper = p / 'wrapper.sh'
            wrapper.write_text('#!/bin/bash\ncp queuedata.json "${CAPTURE}/$(basename "$PWD").json"\nexit 0\n')
            env = dict(os.environ, CAPTURE=directory, EPICPROD_STANDARD_WRAPPER=str(wrapper),
                       EPICPROD_PILOTS_PER_SLOT='2', EPICPROD_PILOT_STAGGER='0',
                       EPICPROD_TIMEFLOOR_SPREAD='0', EPICPROD_ALLOCATION_SECONDS=str(allocation),
                       EPICPROD_DRAIN_RESERVE_SECONDS=str(reserve), EPICPROD_STARTUP_MARGIN_SECONDS='300')
            env.update(extra or {})
            result = subprocess.run(['bash', str(LAUNCHER)], cwd=p, env=env,
                                    capture_output=True, text=True, timeout=10)
            children = [json.loads(f.read_text()) for f in sorted(p.glob('pilot-*.json'))]
            original = json.loads((p / 'queuedata.json').read_text()) if queuedata else None
            return result, children, original

    def test_long_allocation_keeps_successive_job_window_and_final_job_reserve(self):
        result, children, original = self.run_launcher()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(children), 2)
        self.assertEqual(original['timefloor'], 510)
        for child in children:
            self.assertGreaterEqual(child['timefloor'], 509)
            self.assertLessEqual(child['timefloor'], 510)
            self.assertLessEqual(child['maxtime'], 42900)
            self.assertGreaterEqual(child['maxtime'] - child['timefloor'] * 60, 10800)
            self.assertEqual(child['container_options'], 'unchanged')

    def test_new_queue_data_cannot_extend_an_old_short_allocation(self):
        result, children, _ = self.run_launcher(allocation=18000)
        self.assertEqual(result.returncode, 0, result.stderr)
        for child in children:
            self.assertLessEqual(child['maxtime'], 17700)
            self.assertLessEqual(child['timefloor'], 115)
            self.assertGreaterEqual(child['maxtime'] - child['timefloor'] * 60, 10800)

    def test_old_queue_data_remains_a_bound_during_rollout(self):
        result, children, _ = self.run_launcher(maximum=18000, floor=180)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(all(c['maxtime'] <= 17700 for c in children))

    def test_later_child_does_not_restart_allocation_clock(self):
        result, children, _ = self.run_launcher(extra={'EPICPROD_PILOT_STAGGER': '1'})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(children[1]['maxtime'], children[0]['maxtime'])
        self.assertLessEqual(children[1]['timefloor'], children[0]['timefloor'])

    def test_invalid_or_missing_policy_starts_no_wrapper(self):
        for settings in ({'queuedata': False}, {'maximum': 0},
                         {'allocation': 1000}, {'extra': {'EPICPROD_PILOTS_PER_SLOT': 'bad'}}):
            with self.subTest(settings=settings):
                result, children, _ = self.run_launcher(**settings)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(children, [])

    def test_installer_preserves_unrelated_submit_settings_and_is_repeatable(self):
        old = '''executable = /data/atlpan/harvester_common/epicprod-multipilot.sh
transfer_input_files = {executableFile},/data/atlpan/harvester_common/queuedata/UM_GREX_PanDA_1/queuedata.json
environment = "HARVESTER_ID={harvesterID} GTAG={gtag}"
+xcount = 8
+maxMemory = 48000
+maxWallTime = 300
arguments = --piloturl file:///keep-this-canary --harvester-submit-mode PULL
'''
        changed = installer.submit_template(old)
        self.assertIn('+maxWallTime = 720', changed)
        self.assertIn('EPICPROD_ALLOCATION_SECONDS=43200', changed)
        self.assertIn('arguments = --piloturl file:///keep-this-canary --harvester-submit-mode PULL', changed)
        self.assertEqual(installer.submit_template(changed), changed)
        with self.assertRaises(ValueError):
            installer.submit_template(old.replace('+maxWallTime = 300', '+maxWallTime = 999'))


if __name__ == '__main__':
    unittest.main()
