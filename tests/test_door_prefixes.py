"""A catalog source path under /cache/eic/EPIC (the area EIC-XRD serves) reads
exactly as the same path under /volatile/eic/EPIC, the form stored records
carry: same request tail for the EVGEN match, same filters, same task name,
same RECO output DID, same status-grid EVGEN path."""
from types import SimpleNamespace

from django.test import SimpleTestCase

from pcs import commands, services
from swf_epicprod.analytics import status_grids

TAIL = 'DIS/pythia8.316-1.0/NC/noRad/ep/10x100/q2_10to100'
VOLATILE = '/volatile/eic/EPIC/EVGEN/' + TAIL
CACHE = '/cache/eic/EPIC/EVGEN/' + TAIL


class DoorPrefixTests(SimpleTestCase):
    def test_strip_door_prefix(self):
        for p in (VOLATILE, CACHE, VOLATILE.lstrip('/'), CACHE.lstrip('/')):
            with self.subTest(p=p):
                self.assertEqual(services.strip_door_prefix(p), 'EVGEN/' + TAIL)
        for p in ('/EVGEN/' + TAIL, '/other/eic/EPIC/EVGEN/x', ''):
            with self.subTest(p=p):
                self.assertEqual(services.strip_door_prefix(p), p)

    def test_request_input_tail(self):
        self.assertEqual(services._request_input_tail(VOLATILE), TAIL.lower())
        self.assertEqual(services._request_input_tail(CACHE), TAIL.lower())
        self.assertEqual(services._request_input_tail('/EVGEN/' + TAIL), TAIL.lower())
        for p in ('/cache/eic/EPIC/RECO/x/y', '/cache/eic/EPIC/EVGEN', '/other/x/y'):
            with self.subTest(p=p):
                self.assertEqual(services._request_input_tail(p), '')

    def test_csv_filters(self):
        self.assertEqual(services._extract_csv_filters(CACHE, 'epic_craterlake'),
                         services._extract_csv_filters(VOLATILE, 'epic_craterlake'))
        # A path under no door prefix keeps every segment, as before.
        other = '/other/eic/EPIC/EVGEN/' + TAIL
        self.assertEqual(services._extract_csv_filters(other, 'c'),
                         services._extract_csv_filters(other.lstrip('/'), 'c'))

    def test_task_name(self):
        self.assertEqual(services._task_name_from_path(VOLATILE), 'EVGEN/' + TAIL)
        self.assertEqual(services._task_name_from_path(CACHE), 'EVGEN/' + TAIL)

    def test_reco_output_dataset(self):
        def task(loc):
            return SimpleNamespace(
                campaign_id=1, campaign=SimpleNamespace(name='26.07.1'),
                dataset=SimpleNamespace(source_location=loc, detector_config='epic_craterlake',
                                        task_name='flat-name'))
        want = f'/RECO/26.07.1/epic_craterlake/{TAIL}'
        self.assertEqual(commands._output_dataset_name(task(VOLATILE)), want)
        self.assertEqual(commands._output_dataset_name(task(CACHE)), want)
        self.assertEqual(commands._output_dataset_name(task('/other/x')), 'flat-name')

    def test_status_grid_evgen_path(self):
        self.assertEqual(status_grids.evgen_path(CACHE), status_grids.evgen_path(VOLATILE))
        self.assertEqual(status_grids.evgen_path(CACHE), status_grids.evgen_path('epic:/EVGEN/' + TAIL))
