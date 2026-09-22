"""The delivered-file inventory (docs/CAMPAIGN_DELIVERY.md, Nightly
production): a file is delivered when attached to its dataset; the DIDs
a failed upload leaves behind are named under the location but attached
to nothing, and are left out and counted. Over a fake catalog; no
network."""
import json
import os
import sys
import unittest
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from swf_epicprod.analytics import file_events  # noqa: E402

LOC_A = '/RECO/26.07.1/epic_craterlake/EXCLUSIVE/UPSILON/Upsilon1S/9x130/hiAcc'
LOC_B = '/RECO/26.07.1/epic_craterlake/EXCLUSIVE/UPSILON/Upsilon2S/9x130/hiAcc'
LOC_GONE = '/RECO/26.07.1/epic_craterlake/EXCLUSIVE/UPSILON/none/9x130/hiAcc'


class FakeCatalog:
    """``/dids/epic/<dataset>/files`` over a dict of dataset -> names."""

    def __init__(self, attached):
        self.attached = attached
        self.calls = []

    def get(self, path, **query):
        self.calls.append(path)
        assert path.startswith('/dids/epic/') and path.endswith('/files')
        dataset = path[len('/dids/epic/'):-len('/files')]
        if dataset not in self.attached:
            raise urllib.error.HTTPError(path, 404, 'not found', {}, None)
        return '\n'.join(json.dumps({'name': n, 'bytes': 1})
                         for n in self.attached[dataset])


class DeliveredNamesTest(unittest.TestCase):
    def test_unattached_dids_are_left_out_and_counted(self):
        catalog = FakeCatalog({
            LOC_A: {f'{LOC_A}/a.0001.root', f'{LOC_A}/a.0002.root'},
            LOC_B: {f'{LOC_B}/b.0001.root'},
        })
        wanted = {
            f'{LOC_A}/a.0001.root': '26.07',
            f'{LOC_A}/a.0002.root': '26.07',
            f'{LOC_A}/a.0003.root': '26.07',   # a failed upload's DID
            f'{LOC_A}/a.0004.root': '26.07',   # another
            f'{LOC_B}/b.0001.root': '26.07',
        }
        notes = []
        kept, unattached = file_events.delivered_names(
            catalog.get, wanted, log=notes.append)
        self.assertEqual(sorted(kept), [f'{LOC_A}/a.0001.root',
                                        f'{LOC_A}/a.0002.root',
                                        f'{LOC_B}/b.0001.root'])
        self.assertEqual(kept[f'{LOC_B}/b.0001.root'], '26.07')
        self.assertEqual(unattached, {LOC_A: 2})
        # One listing per location, none repeated.
        self.assertEqual(len(catalog.calls), 2)
        self.assertEqual(notes, [])

    def test_location_that_is_no_dataset_counts_as_unattached(self):
        catalog = FakeCatalog({LOC_A: {f'{LOC_A}/a.0001.root'}})
        wanted = {f'{LOC_A}/a.0001.root': '26.07',
                  f'{LOC_GONE}/x.0001.root': '26.07'}
        notes = []
        kept, unattached = file_events.delivered_names(
            catalog.get, wanted, log=notes.append)
        self.assertEqual(list(kept), [f'{LOC_A}/a.0001.root'])
        self.assertEqual(unattached, {LOC_GONE: 1})
        self.assertEqual(len(notes), 1)
        self.assertIn(LOC_GONE, notes[0])

    def test_other_catalog_errors_are_not_swallowed(self):
        def broken(path, **query):
            raise urllib.error.HTTPError(path, 503, 'down', {}, None)
        with self.assertRaises(urllib.error.HTTPError):
            file_events.delivered_names(broken, {f'{LOC_A}/a.root': '26.07'},
                                        log=lambda m: None)


if __name__ == '__main__':
    unittest.main()
