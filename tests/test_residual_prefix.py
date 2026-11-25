"""Residual reruns of a task whose outputs carry a path prefix
(pcs.commands.reco_row_key, _delivered_row_keys, _residual_rows). A
background-mixed task writes under its background tag's prefix,
``RECO/<ver>/<config>/<prefix>/[tryN/]<input dir>/...``, and its
delivered files key onto its manifest rows like an unprefixed task's."""
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

from pcs import manifests
from pcs.commands import _delivered_row_keys, _residual_rows, reco_row_key

DIR = 'DIS/NC/18x275/q2_1'
PREFIX = 'Bkg_Exact1S_2us_e_only/GoldCt/10um'
PLAIN = f'/RECO/26.07.3/epic_craterlake/{DIR}'
MIXED = f'/RECO/26.07.3/epic_craterlake/{PREFIX}/{DIR}'
MIXED_TRY2 = f'/RECO/26.07.3/epic_craterlake/{PREFIX}/try2/{DIR}'


def reco(location, chunk, mark=''):
    return f'{location}/run001.{chunk:04d}{mark}.eicrecon.edm4eic.root'


class PrefixedKeys(unittest.TestCase):

    def test_prefixed_file_keys_onto_the_plain_row(self):
        key = reco_row_key(reco(PLAIN, 7))
        self.assertEqual(key, ('26.07.3', 'epic_craterlake', DIR, 'run001', '0007'))
        self.assertEqual(reco_row_key(reco(MIXED, 7), PREFIX), key)

    def test_try_segment_follows_the_prefix(self):
        key = reco_row_key(reco(PLAIN, 7))
        self.assertEqual(reco_row_key(reco(MIXED_TRY2, 7), PREFIX), key)
        self.assertEqual(reco_row_key(reco(MIXED_TRY2, 7, '.p4614093'), PREFIX), key)

    def test_single_segment_prefix(self):
        name = reco(f'/RECO/26.07.3/epic_craterlake/eicrecon-v1.2.3/try3/{DIR}', 7)
        self.assertEqual(reco_row_key(name, 'eicrecon-v1.2.3'), reco_row_key(reco(PLAIN, 7)))

    def test_slashes_around_the_prefix_do_not_matter(self):
        self.assertEqual(reco_row_key(reco(MIXED, 7), f'/{PREFIX}/'),
                         reco_row_key(reco(PLAIN, 7)))

    def test_name_without_the_prefix_is_keyed_as_it_is(self):
        self.assertEqual(reco_row_key(reco(PLAIN, 7), PREFIX), reco_row_key(reco(PLAIN, 7)))

    def test_prefix_matches_whole_segments_only(self):
        key = reco_row_key(reco(MIXED, 7), 'Bkg_Exact1S')
        self.assertEqual(key[2], f'{PREFIX}/{DIR}')

    def test_no_prefix_given_keeps_the_path_as_before(self):
        self.assertEqual(reco_row_key(reco(MIXED, 7))[2], f'{PREFIX}/{DIR}')
        self.assertEqual(reco_row_key(reco(PLAIN.replace('epic_craterlake', 'epic_craterlake/try2'), 7)),
                         reco_row_key(reco(PLAIN, 7)))

    def test_nothing_below_the_prefix_is_not_a_row(self):
        self.assertIsNone(reco_row_key(
            '/RECO/26.07.3/epic_craterlake/try2.0001.eicrecon.edm4eic.root'))


class PrefixedResidual(unittest.TestCase):

    def test_residual_of_a_background_mixed_task(self):
        # Chunks 0-2 delivered by the first attempt, chunk 3 by try2,
        # chunk 4 never: the residual is chunk 4 alone.
        csv_rows = manifests.format_rows(
            [(f'{DIR}/run001', 'hepmc3.tree.root', 100, c) for c in range(5)])
        names = [reco(MIXED, c) for c in range(3)] + [reco(MIXED_TRY2, 3)]
        keys = {reco_row_key(n, PREFIX)[2:] for n in names}
        with mock.patch('pcs.commands._evgen_env', return_value={'TAG_PREFIX': PREFIX}):
            residual, cov = _residual_rows(None, csv_rows, (keys, [], {}))
        self.assertEqual(residual, [csv_rows[4]])
        self.assertEqual((cov['rows_total'], cov['rows_residual']), (5, 1))

    def test_delivered_keys_leave_the_task_prefix_out(self):
        did = f'epic:{MIXED}'
        names = [reco(MIXED, 0), reco(MIXED, 1), reco(MIXED_TRY2, 2)]
        services = SimpleNamespace(
            fetch_jlab_rucio_did_files=lambda scope, name: [{'name': n} for n in names],
            fetch_jlab_rucio_dataset_arrival=lambda scope, name: [
                {'rse': 'EIC-XRD', 'length': 3, 'available_length': 3}],
            fetch_jlab_rucio_unarrived_files=lambda scope, names: set())
        task = SimpleNamespace(overrides={'outputs': [{'stage': 'RECO', 'did': did}]})
        with mock.patch.dict(sys.modules, {'pcs.services': services}), \
                mock.patch('pcs.commands._evgen_env', return_value={'TAG_PREFIX': PREFIX}):
            keys, dids, arrival = _delivered_row_keys(task)
        self.assertEqual(keys, {(DIR, 'run001', f'{c:04d}') for c in range(3)})
        self.assertEqual(dids, [did])
        self.assertEqual(arrival[did]['registered'], 3)

    def test_unprefixed_task_is_unchanged(self):
        did = f'epic:{PLAIN}'
        services = SimpleNamespace(
            fetch_jlab_rucio_did_files=lambda scope, name: [{'name': reco(PLAIN, 0)}],
            fetch_jlab_rucio_dataset_arrival=lambda scope, name: [
                {'rse': 'EIC-XRD', 'length': 1, 'available_length': 1}],
            fetch_jlab_rucio_unarrived_files=lambda scope, names: set())
        task = SimpleNamespace(overrides={'outputs': [{'stage': 'RECO', 'did': did}]})
        with mock.patch.dict(sys.modules, {'pcs.services': services}), \
                mock.patch('pcs.commands._evgen_env', return_value={}):
            keys, _dids, _arrival = _delivered_row_keys(task)
        self.assertEqual(keys, {(DIR, 'run001', '0000')})


if __name__ == '__main__':
    unittest.main()
