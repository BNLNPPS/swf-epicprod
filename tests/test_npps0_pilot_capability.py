"""npps0's committed pilot pass and queue data supply its image evidence."""
import json
from pathlib import Path
import unittest

from swf_epicprod import npps0_pilots as pilots

ROOT = Path(__file__).resolve().parents[1] / 'tools/npps0'
PASS = (ROOT / 'epicprod-gpu-pilot.sh').read_bytes()
QUEUEDATA = (ROOT / 'config/queuedata.json').read_bytes()


def files(script=PASS, queuedata=QUEUEDATA):
    return lambda url: script if url == pilots.PASS else queuedata


class Npps0Capability(unittest.TestCase):
    def test_committed_pass_serves_the_jobs_own_image(self):
        cap = pilots.read_capability(files())
        self.assertEqual(cap['mode'], 'any', cap)
        self.assertEqual(cap['container_type'], 'apptainer:pilot')

    def test_a_pass_or_queue_data_that_changes_shape_refuses(self):
        data = json.loads(QUEUEDATA)
        queue = data.get(pilots.QUEUE, data)
        queue['container_type'] = 'singularity:wrapper'
        changed = json.dumps(data).encode()
        for fetch in (files(script=PASS.replace(b'QUEUE=BNL_NPPS_GPU', b'QUEUE=OTHER')),
                      files(queuedata=changed), files(queuedata=b'not json')):
            cap = pilots.read_capability(fetch)
            self.assertEqual(cap['mode'], 'unknown', cap)

    def test_unreadable_and_oversized_sources_refuse(self):
        def unavailable(_):
            raise OSError('not reachable')
        for fetch in (unavailable, files(script=b'x' * (pilots.LIMIT + 1))):
            self.assertEqual(pilots.read_capability(fetch)['mode'], 'unknown')

    def test_stale_and_missing_reports_never_authorize(self):
        cap = pilots.read_capability(files())
        self.assertEqual(pilots.observed_capability({'value': cap, 'age_seconds': 1}, 900)['mode'], 'any')
        for age in (None, -1, 901):
            self.assertEqual(pilots.observed_capability({'value': cap, 'age_seconds': age}, 900)['mode'], 'unknown')
        self.assertEqual(pilots.observed_capability({}, 900)['mode'], 'unknown')


if __name__ == '__main__':
    unittest.main()
