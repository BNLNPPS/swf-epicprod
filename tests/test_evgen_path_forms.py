"""The EVGEN registration action's path forms (docs/EPICPROD_EVGEN_INPUTS.md
§ Registration): every accepted spelling reduces to one /EVGEN/... path."""
from django.test import SimpleTestCase

from pcs import services

TAIL = '/EVGEN/EW_BSM/ALP/madgraph5-3.7.0-1.0/aem-axem/10x110/ma_1.0'


class NormalizeEvgenPathTests(SimpleTestCase):
    def test_every_form_reduces_to_the_did_tail(self):
        for raw in (TAIL, TAIL + '/', 'epic:' + TAIL,
                    '/cache/eic/EPIC' + TAIL,
                    '/jlab-osdf-ro/eic/EPIC/cache' + TAIL,
                    'root://dtn-rucio.jlab.org:1094//cache/eic/EPIC' + TAIL,
                    'root://dtn2304.jlab.org:8443//jlab-osdf-ro/eic/EPIC/cache' + TAIL):
            with self.subTest(raw=raw):
                self.assertEqual(services.normalize_evgen_path(raw), TAIL)

    def test_non_evgen_paths_are_refused(self):
        for raw in ('/cache/eic/EPIC/RECO/26.06.0/x/y', '/cache/eic/EPIC/EVGEN',
                    '/other/eic/EPIC' + TAIL, '/volatile/eic/EPIC' + TAIL,
                    '/jlab-osdf-ro/eic/EPIC/volatile' + TAIL):
            with self.subTest(raw=raw), self.assertRaises(services.ServiceError):
                services.normalize_evgen_path(raw)

    def test_door_check_asks_the_cache_area(self):
        self.assertEqual(services.XROOTD_EPIC_PATH, '/jlab-osdf-ro/eic/EPIC/cache')
        self.assertTrue(services.XROOTD_EPIC_BASE.endswith('//jlab-osdf-ro/eic/EPIC/cache'))
