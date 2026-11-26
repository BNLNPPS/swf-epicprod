"""A production sample's output places no grid, whichever task's record
holds it (swf_epicprod.analytics.status_grids.build_status_grids): the
Rucio match has attributed a sample's unrecorded tries to tasks of the
same species and energy."""
import unittest
from types import SimpleNamespace
from unittest import mock

from swf_epicprod.analytics import status_grids

CFG = 'epic_craterlake'
SAMPLE = {'production_sample': {'sample': 'cES000002', 'events': 100}}
BYSTANDER = f'group.EIC.26.07.1.{CFG}.p1107.e52.s9.r9.130to177deg'
SAMPLE_TASK = f'group.EIC.26.07.3.{CFG}.p1107.e37.s11.r11.3to50deg.cES000002'


def queryset(rows=None, objects=None):
    qs = mock.MagicMock()
    qs.order_by.return_value.values_list.return_value = rows or []
    qs.select_related.return_value = objects or []
    return qs


class SampleOutputPlacesNoGrid(unittest.TestCase):

    def build(self, convention):
        editions = [('pc58', f'group.EIC.26.07.1.{CFG}.p1107.e52.s9.r9.130to177deg', {}, '26.07.1'),
                    ('pc6847', SAMPLE_TASK, SAMPLE, '26.07.3')]
        tasks = [SimpleNamespace(composed_name=BYSTANDER, dataset=SimpleNamespace(
                     physics_config=SimpleNamespace(label='pc58'), metadata={})),
                 SimpleNamespace(composed_name=SAMPLE_TASK, dataset=SimpleNamespace(
                     physics_config=SimpleNamespace(label='pc6847'), metadata=SAMPLE))]
        dataset_filter = mock.MagicMock(side_effect=lambda **kw: (
            queryset(objects=[]) if 'metadata__stage' in kw else queryset(rows=editions)))
        completion = {'value': [{'campaign': '26.07', 'pcs': [
            {'pc': 'pc58', 'delivered': 10, 'target': 100}]}], 'built_at': None}
        with mock.patch('pcs.models.Campaign.objects.filter') as campaign, \
                mock.patch('pcs.models.Dataset.objects.filter', dataset_filter), \
                mock.patch('pcs.models.ProdTask.objects.filter', return_value=queryset(objects=tasks)), \
                mock.patch('pcs.services.evgen_registered_paths', return_value={'/EVGEN/x': 1}), \
                mock.patch('pcs.services.evgen_convention_paths_cached', return_value=convention), \
                mock.patch('monitor_app.cached_product.get_product', return_value=completion):
            campaign.return_value.first.return_value = SimpleNamespace(name='26.07')
            return status_grids.build_status_grids('26.07')

    def test_bystander_holding_a_sample_try_places_no_grid(self):
        convention = {
            '/EVGEN/cES000002/try8/SINGLE/neutron/5GeV/3to50deg.cES000002': {'tasks': [BYSTANDER]},
            '/EVGEN/SINGLE/neutron/5GeV/130to177deg': {'tasks': [BYSTANDER]},
        }
        families = [g['family'] for g in self.build(convention)['grids']]
        self.assertEqual(families, ['SINGLE/neutron'])
        self.assertFalse(any('cES' in f for f in families))


if __name__ == '__main__':
    unittest.main()
