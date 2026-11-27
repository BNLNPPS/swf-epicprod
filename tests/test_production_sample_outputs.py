"""A production sample owns every dataset under its own prefix
(pcs.services.production_sample_owners): the Rucio match gives those
datasets to the sample and to no task of the same species and energy
(docs/EPICPROD_DATA_LINEAGE.md, Output ownership)."""
import unittest
from types import SimpleNamespace
from unittest import mock

from pcs import services
from pcs.services import production_sample_output_key, production_sample_owner

CFG = 'epic_craterlake'
SAMPLE_TASK = f'group.EIC.26.07.3.{CFG}.p1107.e37.s11.r11.3to50deg.cES000002'
OWNERS = {('26.07.3', CFG, 'cES000002'): SAMPLE_TASK}


def did(tail, stage='RECO', version='26.07.3'):
    return f'epic:/{stage}/{version}/{CFG}/{tail}'


SAMPLE_TRY8 = did('cES000002/try8/SINGLE/neutron/5GeV/3to50deg.cES000002')
SAMPLE_TRY9 = did('cES000002/try9/SINGLE/neutron/5GeV/3to50deg.cES000002')
PLAIN = did('SINGLE/neutron/5GeV/3to50deg')


class SampleKey(unittest.TestCase):

    def test_key_is_version_config_and_leading_segment(self):
        self.assertEqual(production_sample_output_key(SAMPLE_TRY8),
                         ('26.07.3', CFG, 'cES000002'))

    def test_every_try_of_the_sample_has_its_owner(self):
        for name in (SAMPLE_TRY8, SAMPLE_TRY9,
                     did('cES000002/SINGLE/neutron/5GeV/3to50deg.cES000002'),
                     did('cES000002/try5/SINGLE/neutron/5GeV/3to50deg.cES000002', stage='FULL')):
            self.assertEqual(production_sample_owner(name, OWNERS), SAMPLE_TASK, name)

    def test_other_outputs_have_none(self):
        for name in (PLAIN,
                     did('cES000001/SINGLE/neutron/5GeV/3to50deg.cES000001'),
                     did('cES000002/try8/SINGLE/neutron/5GeV/3to50deg.cES000002', version='26.07.1'),
                     'epic:/RECO/26.07.3', ''):
            self.assertEqual(production_sample_owner(name, OWNERS), '', name)


def task(name, filters):
    saved = []
    t = SimpleNamespace(name=name, overrides={'csv_import': {'filters': filters}},
                        dataset=SimpleNamespace(metadata={}, detector_config=CFG))
    t.save = lambda **kw: saved.append(kw)
    return t


def snapshot(*dids):
    return {'fetched_at': '2026-11-26T12:00:00Z',
            'campaigns': {'/RECO/26.07.3': {'datasets': [
                {'did': d, 'length': 0, 'bytes': 0, 'rse_replicas': []} for d in dids]}}}


class MatchGivesSampleOutputsToTheSample(unittest.TestCase):

    def run_match(self, tasks, snap):
        campaign = SimpleNamespace(name='26.07', data={}, save=lambda **kw: None)
        qs = mock.MagicMock()
        qs.select_related.return_value = tasks
        with mock.patch.object(services.ProdTask.objects, 'filter', return_value=qs), \
                mock.patch.object(services, '_output_owner_names', return_value={}), \
                mock.patch.object(services, 'precreated_output_owners', return_value={}), \
                mock.patch.object(services, 'production_sample_owners', return_value=OWNERS), \
                mock.patch.object(services, 'consolidate_output_ownership', return_value={}):
            services.match_requests_to_rucio_snapshot(snap, campaign=campaign)
        return {t.name: t.overrides for t in tasks}

    def test_sample_tries_go_to_the_sample_and_not_to_a_task_of_the_same_physics(self):
        gun = {'physics': 'SINGLE', 'species': 'neutron', 'energy': '5GeV',
               'beam': '', 'q2': '', 'detector': CFG}
        bystander = task(f'group.EIC.26.02.0.{CFG}.p1107.e37.s0.r0.130to177deg', gun)
        sample = task(SAMPLE_TASK, {})
        out = self.run_match([bystander, sample], snapshot(SAMPLE_TRY8, SAMPLE_TRY9, PLAIN))

        # The filters still match the plain dataset, so the bystander is a
        # task the filters would have handed the sample's tries to.
        self.assertEqual([o['did'] for o in out[bystander.name]['outputs']], [PLAIN])
        self.assertEqual(out[bystander.name]['output_refs'], [])
        self.assertEqual(sorted(o['did'] for o in out[SAMPLE_TASK]['outputs']),
                         sorted([SAMPLE_TRY8, SAMPLE_TRY9]))


class ReconcileGivesSampleOutputsToTheSample(unittest.TestCase):
    """An unowned try of a sample (cES000002/try8 became past output
    group.EIC.26.07.3.epic_craterlake.p1107.e52.s11.r11, task 8857) is
    attached to the sample task, and no past-output row is created."""

    def test_unowned_sample_try_attaches_to_the_sample(self):
        from pcs import reconcile
        sample_task = SimpleNamespace(name=SAMPLE_TASK)
        campaign = SimpleNamespace(name='26.07')
        task_qs = mock.MagicMock()
        task_qs.order_by.return_value.first.return_value = sample_task
        empty = mock.MagicMock()
        empty.__iter__.return_value = iter([])
        empty.select_related.return_value.order_by.return_value.distinct.return_value = []
        upserts = []
        with mock.patch.object(reconcile.Campaign.objects, 'get', return_value=campaign), \
                mock.patch.object(reconcile.ProdTask.objects, 'filter', return_value=task_qs), \
                mock.patch.object(reconcile.Dataset.objects, 'filter', return_value=empty), \
                mock.patch.object(reconcile.Dataset.objects, 'get_or_create') as create_row, \
                mock.patch.object(reconcile.ProdTask.objects, 'create') as create_task, \
                mock.patch.object(reconcile, 'group_editions', return_value={}), \
                mock.patch.object(reconcile, '_upsert_task_output',
                                  side_effect=lambda task, entry: upserts.append((task.name, entry['did']))), \
                mock.patch.object(services, 'load_rucio_snapshot', return_value=snapshot(SAMPLE_TRY8)), \
                mock.patch.object(services, '_ensure_csvimport_anchors', return_value=(None,) * 6), \
                mock.patch.object(services, 'precreated_output_owners', return_value={}), \
                mock.patch.object(services, 'production_sample_owners', return_value=OWNERS), \
                mock.patch.object(services, 'consolidate_output_ownership', return_value={}), \
                mock.patch('monitor_app.epicprod_logging.log_epicprod_action'), \
                mock.patch('monitor_app.models.PersistentState.get_state', return_value={}), \
                mock.patch('monitor_app.models.PersistentState.update_state'):
            result = reconcile.reconcile_campaign_from_rucio('26.07')
        self.assertEqual(upserts, [(SAMPLE_TASK, SAMPLE_TRY8)])
        create_row.assert_not_called()
        create_task.assert_not_called()
        self.assertEqual((result.get('attached'), result.get('created')), (1, 0))


if __name__ == '__main__':
    unittest.main()
