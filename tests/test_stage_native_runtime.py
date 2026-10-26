"""Exercise the assembled helper interface that failed on the real GKE worker."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('stage_runtime', ROOT / 'scripts/stage-native-runtime.py')
stage_runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stage_runtime)


def pilot_archive(*members):
    """A pilot archive holding the named members, as the frozen baseline has one."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
        for name in members:
            info = tarfile.TarInfo(name)
            archive.addfile(info, io.BytesIO(b''))
    return buffer.getvalue()


class RuntimeBundle(unittest.TestCase):
    def test_committed_helpers_ignore_stale_baseline_and_prepare_queuedata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = root / 'baseline'; baseline.mkdir()
            for name in stage_runtime.ASSETS:
                (baseline / name).write_bytes(b'frozen asset')
            (baseline / 'pilot3-native.tar.gz').write_bytes(pilot_archive('pilot3/pilot.py'))
            (baseline / 'queuedata-source.json').write_text('{"nickname":"BNL_OSG_PanDA_1"}')
            (baseline / 'runtime-policy.py').write_text('obsolete helper')
            (baseline / 'private-key').write_text('must not be copied')
            bundle = root / 'bundle'
            digest = hashlib.sha256((baseline / 'pilot3-native.tar.gz').read_bytes()).hexdigest()
            manifest = stage_runtime.stage(baseline, bundle, digest)
            self.assertFalse((bundle / 'private-key').exists())
            self.assertNotIn('es_batch_channel', manifest)
            for name, expected in manifest['files'].items():
                self.assertEqual(hashlib.sha256((bundle / name).read_bytes()).hexdigest(), expected)
            queuedata = root / 'source.json'
            queuedata.write_text(json.dumps({'panda_queue': 'BNL_OSG_PanDA_1', 'other': 42}))
            result = subprocess.run([
                sys.executable, str(bundle / 'runtime-policy.py'), '--policy',
                str(bundle / 'queue-runtime-policy.json'), '--queue', 'BNL_OSG_PanDA_1',
                '--prepare-queuedata', str(queuedata), '--output', str(root / 'out.json'),
            ], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads((root / 'out.json').read_text()),
                             {'panda_queue': 'BNL_OSG_PanDA_1', 'other': 42, 'maxtime': 36000})
            with self.assertRaises(FileExistsError):
                stage_runtime.stage(baseline, bundle, digest)

    def test_batch_pilot_is_declared(self):
        with tempfile.TemporaryDirectory() as directory:
            baseline = Path(directory)
            for name in stage_runtime.ASSETS:
                (baseline / name).write_bytes(b'frozen asset')
            archive = pilot_archive('pilot3/pilot.py', stage_runtime.ES_BATCH_MODULE)
            (baseline / 'pilot3-native.tar.gz').write_bytes(archive)
            (baseline / 'queuedata-source.json').write_text('{"nickname":"BNL_ePIC_GOOGLE_es"}')
            manifest = stage_runtime.stage(baseline, baseline / 'bundle',
                                           hashlib.sha256(archive).hexdigest())
            self.assertEqual(manifest['es_batch_channel'], {'stageout_s': 300})

    def test_wrong_archive_is_rejected_before_creating_output(self):
        with tempfile.TemporaryDirectory() as directory:
            baseline = Path(directory)
            for name in stage_runtime.ASSETS:
                (baseline / name).write_bytes(b'frozen asset')
            output = baseline / 'bundle'
            with self.assertRaisesRegex(ValueError, 'approved digest'):
                stage_runtime.stage(baseline, output, 'wrong')
            self.assertFalse(output.exists())
