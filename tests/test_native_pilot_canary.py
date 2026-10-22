"""Execution and image-isolation checks for the opt-in native pilot canary."""
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1] / 'pilot/native-canary'
SPEC = importlib.util.spec_from_file_location('native_container', ROOT / 'native_container.py')
native = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(native)


class NativePilotCanaryTests(unittest.TestCase):
    def test_matching_image_reuses_environment_and_sees_replaced_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            image = work / 'campaign-image'
            image.mkdir()
            proxy = work / 'pilot-proxy'
            proxy.write_text('before')
            job = SimpleNamespace(imagename=str(image), workdir=str(work), jobid=12)
            env = dict(APPTAINER_CONTAINER=str(image), X509_USER_PROXY=str(proxy))
            with patch.dict(os.environ, env):
                command = native.wrapper('export PILOT_X509_USER_PROXY="$X509_USER_PROXY"; '
                                         'test "$(cat "$PILOT_X509_USER_PROXY")" = after', job=job)
                refreshed = work / 'refreshed'
                refreshed.write_text('after')
                refreshed.replace(proxy)
                result = subprocess.run(['bash', '-c', command], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn('apptainer exec', command)
            self.assertNotIn('atlasLocalSetup', command)

    def test_another_campaign_is_rejected_before_payload_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            old, wanted = work / 'old/image', work / 'wanted/image'
            old.mkdir(parents=True)
            wanted.mkdir(parents=True)
            job = SimpleNamespace(imagename=str(wanted), workdir=str(work), jobid=12)
            with patch.dict(os.environ, {'APPTAINER_CONTAINER': str(old)}):
                with self.assertRaisesRegex(RuntimeError, 'mismatched image'):
                    native.wrapper('touch should-not-run', job=job)
            self.assertFalse((work / 'should-not-run').exists())
            self.assertFalse(native.same_image('image', str(wanted)))

    def test_launcher_refuses_an_unexpected_outer_image(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            for name in ('old', 'wanted'):
                (work / name).mkdir()
            (work / 'pilot-proxy').write_text('synthetic-test-credential')
            env = dict(os.environ, APPTAINER_CONTAINER=str(work / 'old'),
                       EPICPROD_CANARY_IMAGE=str(work / 'wanted'),
                       EPICPROD_CANARY_TASK_ID='1', EPICPROD_CANARY_QUEUE='test',
                       EPICPROD_CANARY_ARCHIVE='unused')
            result = subprocess.run(['bash', str(ROOT / 'epic-native-canary.sh')],
                                    cwd=work, env=env, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 65)
            self.assertIn('refusing nested launch', result.stderr)


if __name__ == '__main__':
    unittest.main()
