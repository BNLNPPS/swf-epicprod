"""The external Harvester's public launcher supplies bounded image evidence."""
from pathlib import Path
import unittest

from swf_epicprod import perlmutter_pilots as pilots

SOURCE = (Path(__file__).resolve().parents[1] /
          'perlmutter/NERSC_Perlmutter_epic_es/epicprod-perlmutter-pilot-launch.sh').read_bytes()


class PerlmutterCapability(unittest.TestCase):
    def test_actual_adapter_binding_and_runtime_hashes(self):
        cap = pilots.read_capability(lambda _: SOURCE)
        self.assertEqual(cap['mode'], 'bound')
        self.assertEqual(cap['images'], ['/cvmfs/singularity.opensciencegrid.org/eicweb/eic_xl:26.07.1-stable'])
        self.assertEqual(cap['pilot_archive_sha256'],
                         '9532f30b41a8b1c8cfcb730eb325933c71c486454ed4365cd3e2d8b625a27ae2')
        self.assertEqual(len(cap['runtime_sha256']), 64)

    def test_unresolved_or_conflicting_image_is_unknown(self):
        for source in (SOURCE.replace(b'IMAGE=/cvmfs/', b'IMAGE=$UNRESOLVED/cvmfs/'),
                       SOURCE + b'\nIMAGE=/cvmfs/second-image\n',
                       SOURCE.replace(b'export EPICPROD_NATIVE_BATCH=1', b'')):
            cap = pilots.read_capability(lambda _: source)
            self.assertEqual(cap['mode'], 'unknown', cap)
            self.assertEqual(cap['images'], [])

    def test_unreadable_and_oversized_adapter_refuse(self):
        def unavailable(_):
            raise OSError('not reachable')
        for fetch in (unavailable, lambda _: b'x' * (128 * 1024 + 1)):
            cap = pilots.read_capability(fetch)
            self.assertEqual(cap['mode'], 'unknown')
            self.assertEqual(cap['images'], [])

    def test_stale_and_missing_reports_never_authorize(self):
        cap = pilots.read_capability(lambda _: SOURCE)
        fresh = pilots.observed_capability({'value': cap, 'age_seconds': 1}, 900)
        self.assertEqual(fresh['mode'], 'bound')
        for age in (None, -1, 901):
            observed = pilots.observed_capability({'value': cap, 'age_seconds': age}, 900)
            self.assertEqual(observed['mode'], 'unknown')
        self.assertEqual(pilots.observed_capability({}, 900)['mode'], 'unknown')
