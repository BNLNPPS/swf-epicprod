"""swf_epicprod.door_paths: a door path under EIC-XRD's prefix reduces to
its EVGEN tail; a path under any other prefix is left alone."""
import unittest

from swf_epicprod import door_paths as dp

TAIL = 'DIS/pythia8.316-1.0/NC/noRad/ep/10x100/q2_10to100'
CACHE = '/cache/eic/EPIC/EVGEN/' + TAIL
OTHERS = ('/EVGEN/' + TAIL, '/volatile/eic/EPIC/EVGEN/' + TAIL,
          '/other/eic/EPIC/EVGEN/x', '/cache/eic/EPICX/EVGEN/x', '')


class DoorPathTests(unittest.TestCase):
    def test_split_and_strip(self):
        for p in (CACHE, CACHE.lstrip('/')):
            with self.subTest(p=p):
                self.assertEqual(dp.split_door_prefix(p), (dp.DOOR_PREFIX, '/EVGEN/' + TAIL))
                self.assertEqual(dp.strip_door_prefix(p), 'EVGEN/' + TAIL)
        for p in OTHERS:
            with self.subTest(p=p):
                self.assertEqual(dp.split_door_prefix(p), ('', p))
                self.assertEqual(dp.strip_door_prefix(p), p)

    def test_split_under_given_prefixes(self):
        osdf = dp.OSDF_PATH + '/EVGEN/' + TAIL
        self.assertEqual(dp.split_door_prefix(osdf), ('', osdf))
        self.assertEqual(dp.split_door_prefix(osdf, (dp.OSDF_PATH, dp.DOOR_PREFIX)),
                         (dp.OSDF_PATH, '/EVGEN/' + TAIL))

    def test_evgen_tail(self):
        self.assertEqual(dp.evgen_tail(CACHE), TAIL)
        self.assertEqual(dp.evgen_tail('/cache/eic/EPIC/EVGEN'), '')
        for p in OTHERS + ('/cache/eic/EPIC/RECO/x/y', None):
            with self.subTest(p=p):
                self.assertIsNone(dp.evgen_tail(p))

    def test_read_base(self):
        self.assertEqual(dp.DOOR_PREFIX, '/cache/eic/EPIC')
        self.assertEqual(dp.OSDF_BASE, 'root://dtn2304.jlab.org:8443//jlab-osdf-ro/eic/EPIC/cache')


if __name__ == '__main__':
    unittest.main()
