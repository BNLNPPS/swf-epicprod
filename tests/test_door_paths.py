"""swf_epicprod.door_paths: a door path under /cache/eic/EPIC and the same
path under /volatile/eic/EPIC reduce to one EVGEN tail."""
import unittest

from swf_epicprod import door_paths as dp

TAIL = 'DIS/pythia8.316-1.0/NC/noRad/ep/10x100/q2_10to100'
VOLATILE = '/volatile/eic/EPIC/EVGEN/' + TAIL
CACHE = '/cache/eic/EPIC/EVGEN/' + TAIL


class DoorPathTests(unittest.TestCase):
    def test_split_and_strip(self):
        for p in (VOLATILE, CACHE, VOLATILE.lstrip('/'), CACHE.lstrip('/')):
            with self.subTest(p=p):
                self.assertEqual(dp.split_door_prefix(p)[1], '/EVGEN/' + TAIL)
                self.assertEqual(dp.strip_door_prefix(p), 'EVGEN/' + TAIL)
        for p in ('/EVGEN/' + TAIL, '/other/eic/EPIC/EVGEN/x', '/cache/eic/EPICX/EVGEN/x', ''):
            with self.subTest(p=p):
                self.assertEqual(dp.split_door_prefix(p), ('', p))
                self.assertEqual(dp.strip_door_prefix(p), p)

    def test_evgen_tail(self):
        self.assertEqual(dp.evgen_tail(VOLATILE), TAIL)
        self.assertEqual(dp.evgen_tail(CACHE), TAIL)
        self.assertEqual(dp.evgen_tail('/cache/eic/EPIC/EVGEN'), '')
        for p in ('/EVGEN/' + TAIL, '/cache/eic/EPIC/RECO/x/y', '/other/x', '', None):
            with self.subTest(p=p):
                self.assertIsNone(dp.evgen_tail(p))

    def test_door_forms(self):
        self.assertEqual(dp.door_forms(VOLATILE), (CACHE, VOLATILE))
        self.assertEqual(dp.door_forms(CACHE), (CACHE, VOLATILE))
        for p in ('/EVGEN/' + TAIL, VOLATILE.lstrip('/'), 'campaign/input.csv', ''):
            with self.subTest(p=p):
                self.assertEqual(dp.door_forms(p), (p,))

    def test_read_base(self):
        self.assertEqual(dp.OSDF_BASE, 'root://dtn2304.jlab.org:8443//jlab-osdf-ro/eic/EPIC/cache')
        self.assertEqual(dp.OSDF_PATH, dp.OSDF_PATHS[0])


if __name__ == '__main__':
    unittest.main()
