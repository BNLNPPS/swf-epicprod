"""Image declarations remain optional and preserve existing dispatch constraints."""
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('native_builder', ROOT / 'pilot/native-canary/build.py')
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


class ImageDispatchTests(unittest.TestCase):
    def test_per_job_launcher_omits_dispatch_image_and_rejects_disabled_container_launching(self):
        launcher = ROOT / 'pilot/native/epic-native-pilot.sh'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'image').mkdir()
            env = dict(os.environ, EPICPROD_NATIVE_IMAGE=str(root / 'image'),
                       APPTAINER_CONTAINER=str(root / 'image'), EPICPROD_NATIVE_ARCHIVE='unused',
                       X509_USER_PROXY=str(root / 'missing'), PILOT_CONTAINER_NAME='/stale/image',
                       EPICPROD_NATIVE_DISPATCH='per-job', EPICPROD_NATIVE_NESTING='on')
            result = subprocess.run(['bash', str(launcher)], cwd=root, env=env,
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 67)
            self.assertIn('dispatch per-job images', result.stdout)
            self.assertNotIn('container_name=', result.stdout)
            env['EPICPROD_NATIVE_NESTING'] = 'off'
            result = subprocess.run(['bash', str(launcher)], cwd=root, env=env,
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 66)
            self.assertIn('requires payload container launching', result.stderr)

    def test_request_preserves_constraints_and_omits_empty_image(self):
        baseline = "def get_dispatcher_dictionary():\n    data = dict(task_id=123, resource_type='SCORE')\n    return data\n"
        ns = {'os': os}
        exec(builder.add_image_dispatch(baseline), ns)
        for value in (None, '', '  ', '/cvmfs/image:26.07.1', '/no/matching/jobs'):
            with self.subTest(value=value), patch.dict(os.environ, {}, clear=True):
                if value is not None:
                    os.environ['PILOT_CONTAINER_NAME'] = value
                expected = dict(task_id=123, resource_type='SCORE')
                if value and value.strip():
                    expected['container_name'] = value.strip()
                self.assertEqual(ns['get_dispatcher_dictionary'](), expected)

    def test_launcher_declares_configured_alias_only_after_image_check(self):
        launcher = ROOT / 'pilot/native/epic-native-pilot.sh'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'actual').mkdir()
            (root / 'alias').symlink_to(root / 'actual')
            (root / 'other').mkdir()
            for actual, expected_code in (('actual', 67), ('other', 65)):
                env = dict(os.environ, EPICPROD_NATIVE_IMAGE=str(root / 'alias'),
                           APPTAINER_CONTAINER=str(root / actual), EPICPROD_NATIVE_ARCHIVE='unused',
                           X509_USER_PROXY=str(root / 'missing'), PILOT_CONTAINER_NAME='/untrusted/override')
                result = subprocess.run(['bash', str(launcher)], cwd=root, env=env,
                                        capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, expected_code)
                if actual == 'actual':
                    self.assertIn('container_name=' + str(root / 'alias'), result.stdout)
                    self.assertNotIn('/untrusted/override', result.stdout)
                else:
                    self.assertNotIn('dispatch container_name=', result.stdout)


if __name__ == '__main__':
    unittest.main()
