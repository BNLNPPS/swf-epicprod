"""Exercise the assembled helper interface that failed on the real GKE worker."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('stage_runtime', ROOT / 'scripts/stage-native-runtime.py')
stage_runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stage_runtime)


class RuntimeBundle(unittest.TestCase):
    def test_committed_helpers_ignore_stale_baseline_and_prepare_queuedata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = root / 'baseline'; baseline.mkdir()
            for name in stage_runtime.ASSETS:
                (baseline / name).write_bytes(b'frozen asset')
            (baseline / 'runtime-policy.py').write_text('obsolete helper')
            (baseline / 'private-key').write_text('must not be copied')
            bundle = root / 'bundle'
            digest = hashlib.sha256(b'frozen asset').hexdigest()
            manifest = stage_runtime.stage(baseline, bundle, digest)
            self.assertFalse((bundle / 'private-key').exists())
            for name, expected in manifest['files'].items():
                self.assertEqual(hashlib.sha256((bundle / name).read_bytes()).hexdigest(), expected)
            queuedata = root / 'source.json'
            queuedata.write_text(json.dumps({'panda_queue': 'BNL_OSG_PanDA_1', 'other': 42}))
            result = subprocess.run([
                sys.executable, str(bundle / 'runtime-policy.py'), '--policy',
                str(bundle / 'queue-runtime-policy.json'), '--queue', 'BNL_OSG_PanDA_1',
                '--prepare-queuedata', queuedata.as_uri(), '--output', str(root / 'out.json'),
            ], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads((root / 'out.json').read_text()),
                             {'panda_queue': 'BNL_OSG_PanDA_1', 'other': 42, 'maxtime': 36000})
            with self.assertRaises(FileExistsError):
                stage_runtime.stage(baseline, bundle, digest)

    def test_wrong_archive_is_rejected_before_creating_output(self):
        with tempfile.TemporaryDirectory() as directory:
            baseline = Path(directory)
            for name in stage_runtime.ASSETS:
                (baseline / name).write_bytes(b'frozen asset')
            output = baseline / 'bundle'
            with self.assertRaisesRegex(ValueError, 'approved digest'):
                stage_runtime.stage(baseline, output, 'wrong')
            self.assertFalse(output.exists())
