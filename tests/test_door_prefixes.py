"""What PCS reads from a catalog source path under EIC-XRD's prefix,
/cache/eic/EPIC: the request tail for the EVGEN match, the filters, the task
name, the RECO output DID and the status-grid EVGEN path
(swf_epicprod.door_paths; its own cases are in test_door_paths.py). A path
under any other prefix, the former /volatile/eic/EPIC among them, is not a
door path."""
from types import SimpleNamespace

from django.test import SimpleTestCase

from pcs import commands, services
from swf_epicprod.analytics import status_grids

TAIL = 'DIS/pythia8.316-1.0/NC/noRad/ep/10x100/q2_10to100'
CACHE = '/cache/eic/EPIC/EVGEN/' + TAIL
OTHERS = ('/volatile/eic/EPIC/EVGEN/' + TAIL, '/other/eic/EPIC/EVGEN/' + TAIL)


class DoorPrefixTests(SimpleTestCase):
    def test_request_input_tail(self):
        self.assertEqual(services._request_input_tail(CACHE), TAIL.lower())
        self.assertEqual(services._request_input_tail('/EVGEN/' + TAIL), TAIL.lower())
        for p in ('/cache/eic/EPIC/RECO/x/y', '/cache/eic/EPIC/EVGEN') + OTHERS:
            with self.subTest(p=p):
                self.assertEqual(services._request_input_tail(p), '')

    def test_csv_filters(self):
        self.assertEqual(services._extract_csv_filters(CACHE, 'c'),
                         services._extract_csv_filters(CACHE.lstrip('/'), 'c'))
        # A path under no door prefix keeps every segment.
        for other in OTHERS:
            with self.subTest(other=other):
                self.assertNotEqual(services._extract_csv_filters(other, 'c'),
                                    services._extract_csv_filters(CACHE, 'c'))

    def test_task_name(self):
        self.assertEqual(services._task_name_from_path(CACHE), 'EVGEN/' + TAIL)
        for other in OTHERS:
            with self.subTest(other=other):
                self.assertEqual(services._task_name_from_path(other), other)

    def test_reco_output_dataset(self):
        def task(loc):
            return SimpleNamespace(
                campaign_id=1, campaign=SimpleNamespace(name='26.07.1'),
                dataset=SimpleNamespace(source_location=loc, detector_config='epic_craterlake',
                                        task_name='flat-name'))
        self.assertEqual(commands._output_dataset_name(task(CACHE)),
                         f'/RECO/26.07.1/epic_craterlake/{TAIL}')
        for other in OTHERS + ('/other/x',):
            with self.subTest(other=other):
                self.assertEqual(commands._output_dataset_name(task(other)), 'flat-name')

    def test_status_grid_evgen_path(self):
        self.assertEqual(status_grids.evgen_path(CACHE), '/EVGEN/' + TAIL)
        self.assertEqual(status_grids.evgen_path(CACHE), status_grids.evgen_path('epic:/EVGEN/' + TAIL))
