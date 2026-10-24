"""The queue registry (docs/QUEUE_REGISTRY.md): one record of each
production queue's status, derived unchanged from the settings it replaces
until adopted, and every reader's answer taken from it."""
import copy
import unittest
from unittest.mock import patch

from swf_epicprod import front, queues as registry
from swf_epicprod.regulation import CAPACITY_POOLS, RETIRED_QUEUES, require_task_destination

# SysConfig as it stood on 2026-10-23.
LIVE = {
    'front.queues': list(registry.SEED_FRONT_QUEUES),
    'front.closed_queues': dict(registry.SEED_CLOSED, BNL_OSG_EPIC_PROD_1=(
        'retired: production moved to BNL_OSG_PanDA_1 on 2026-10-21 (Torre Wenaus, 2026-10-22)')),
    'comp.sites': copy.deepcopy(registry.SEED_ROOM),
}
OLD_RETIRED = {'BNL_OSG_EPIC_PROD_1': registry.SEED_RETIRED['BNL_OSG_EPIC_PROD_1']}


class Registry(unittest.TestCase):
    def config(self, data):
        registry.invalidate()
        self.addCleanup(registry.invalidate)
        p = patch.object(registry, '_config', return_value=data)
        p.start()
        self.addCleanup(p.stop)

    def test_derived_record_gives_every_reader_its_old_answer(self):
        self.config(copy.deepcopy(LIVE))
        self.assertEqual(front.regulated_queues(), LIVE['front.queues'])
        closed = dict(LIVE['front.closed_queues'], **OLD_RETIRED)
        self.assertEqual(front.closed_queues(), closed)
        self.assertEqual(dict(RETIRED_QUEUES), OLD_RETIRED)
        self.assertEqual(dict(CAPACITY_POOLS), {'osg': ('BNL_OSG_PanDA_1', 'BNL_OSG_EPIC_PROD_1')})
        self.assertEqual(registry.jedi_pools(), 'osg:BNL_OSG_PanDA_1;osg_drain:BNL_OSG_EPIC_PROD_1')
        self.assertEqual(registry.status('JLAB_PanDA_1'), 'commissioning')
        self.assertNotIn('JLAB_PanDA_1', front.closed_queues())
        self.assertEqual(registry.status('ComputeCanada_PanDA_1'), 'not_built')
        rooms = registry.room_queues()
        self.assertEqual([e['queue'] for e in rooms['nersc']],
                         ['NERSC_Perlmutter_epic', 'NERSC_Perlmutter_epic_es'])
        self.assertNotIn('BNL_OSG_EPIC_PROD_1', [e['queue'] for e in rooms['osg']])

    def test_live_views_behave_as_the_constants_did(self):
        self.config({})
        self.assertIn('BNL_OSG_EPIC_PROD_1', RETIRED_QUEUES)
        self.assertNotIn('BNL_OSG_PanDA_1', RETIRED_QUEUES)
        self.assertEqual(len(RETIRED_QUEUES), 1)
        self.assertEqual(list(RETIRED_QUEUES), ['BNL_OSG_EPIC_PROD_1'])
        merged = {'Q': 'x'}
        merged.update(RETIRED_QUEUES)
        self.assertIn('BNL_OSG_EPIC_PROD_1', merged)
        self.assertEqual(RETIRED_QUEUES.get('nope', ''), '')
        with self.assertRaisesRegex(ValueError, 'existing tasks drain in place'):
            require_task_destination('BNL_OSG_EPIC_PROD_1')
        require_task_destination('BNL_OSG_PanDA_1')

    def test_restored_or_malformed_settings_cannot_reopen_a_retired_queue(self):
        self.config({'front.queues': ['BNL_OSG_EPIC_PROD_1', 'BNL_OSG_PanDA_1']})
        self.assertEqual(front.regulated_queues(), ['BNL_OSG_PanDA_1'])
        self.assertIn('BNL_OSG_EPIC_PROD_1', front.closed_queues())
        self.config({'front.queues': 'bad', 'front.closed_queues': ['bad']})
        self.assertEqual(front.regulated_queues(), registry.SEED_FRONT_QUEUES)
        self.assertIn('NERSC_Perlmutter_epic', front.closed_queues())
        self.config({registry.KEY: [{'queue': 'Q'}]})
        self.assertIn('BNL_OSG_EPIC_PROD_1', front.closed_queues())

    def test_adoption_writes_the_derived_record_and_keeps_what_it_replaced(self):
        data = copy.deepcopy(LIVE)
        record, adopted = registry.adopt_into(data, 'swf-1')
        self.assertTrue(adopted)
        self.assertEqual(record, registry.derive(LIVE))
        for key in registry.REPLACED_KEYS:
            self.assertNotIn(key, data)
            self.assertEqual(data[registry.ARCHIVE_KEY][key], LIVE[key])
        self.assertIn('queues', data['comp.sites'][0])
        again, adopted = registry.adopt_into(data, 'swf-1', strip_room=True)
        self.assertFalse(adopted)
        self.assertEqual(again, record)
        self.assertNotIn('queues', data['comp.sites'][0])
        self.assertEqual(data[registry.ARCHIVE_KEY]['comp.sites'], LIVE['comp.sites'])
        self.config(data)
        self.assertEqual(front.regulated_queues(), LIVE['front.queues'])
        self.assertEqual(registry.room_queues()['grex'][0]['queue'], 'UM_GREX_PanDA_1')
        with self.assertRaisesRegex(ValueError, 'malformed'):
            registry.adopt_into({registry.KEY: 'bad'}, 'swf-1')

    def test_one_change_retires_a_queue_everywhere(self):
        data = copy.deepcopy(LIVE)
        record, _ = registry.adopt_into(data, 'swf-1')
        before = registry.change(record, 'UM_GREX_PanDA_1', 'retired', reason='gone', by='Torre')
        self.assertEqual(before, 'production')
        self.config(data)
        self.assertNotIn('UM_GREX_PanDA_1', front.regulated_queues())
        self.assertEqual(front.closed_queues()['UM_GREX_PanDA_1'], 'gone')
        self.assertIn('UM_GREX_PanDA_1', RETIRED_QUEUES)
        self.assertNotIn('grex', registry.room_queues())
        registry.change(record, 'NERSC_Perlmutter_epic', 'production', reason='ignored', by='nersc')
        registry.invalidate()
        self.assertNotIn('NERSC_Perlmutter_epic', front.closed_queues())
        self.assertEqual(registry.get('NERSC_Perlmutter_epic')['reason'], '')
        registry.change(record, 'NEW_Q', 'commissioning', by='swf-1', site='jlab')
        registry.invalidate()
        self.assertEqual(registry.get('NEW_Q')['site'], 'jlab')
        with self.assertRaises(ValueError):
            registry.change(record, 'NEW_Q', 'closed')


class NerscSwitches(unittest.TestCase):
    def test_balance_closes_and_reopens_through_the_record(self):
        from swf_epicprod import nersc_allocation as na
        data = copy.deepcopy(LIVE)
        record, _ = registry.adopt_into(data, 'swf-1')

        def fake_write(mutate, by):
            return mutate(record)

        balance = {'projects': {'m3763': {'cpu': {'allocation': 100, 'usage': 10}}}}
        settings = {'nersc.queues': {'NERSC_Perlmutter_epic': 'm3763'}, 'nersc.close_at_used': 0.9}
        with patch('monitor_app.models.SysConfig.get_setting', side_effect=lambda k, d=None: settings[k]), \
             patch.object(registry, 'write', side_effect=fake_write), \
             patch('monitor_app.epicprod_logging.log_epicprod_action'):
            changes = na.apply_queue_switches(balance, 'nersc_allocation')
        self.assertEqual([c['action'] for c in changes], ['open'])
        self.assertEqual(next(e for e in record if e['queue'] == 'NERSC_Perlmutter_epic')['status'],
                         'production')
        balance['projects']['m3763']['cpu']['usage'] = 95
        with patch('monitor_app.models.SysConfig.get_setting', side_effect=lambda k, d=None: settings[k]), \
             patch.object(registry, 'write', side_effect=fake_write), \
             patch('monitor_app.epicprod_logging.log_epicprod_action'):
            changes = na.apply_queue_switches(balance, 'nersc_allocation')
        e = next(e for e in record if e['queue'] == 'NERSC_Perlmutter_epic')
        self.assertEqual((e['status'], e['reason']), ('off', 'NERSC allocation: m3763 CPU 90% or more used'))


class Drift(unittest.TestCase):
    def test_copies_that_disagree_with_the_record_are_named(self):
        record = registry.derive(LIVE)
        ok = {
            'osgsub01-harvester': {'record': {'queue_status': {'status': {
                'BNL_OSG_PanDA_1': None, 'BNL_OSG_EPIC_PROD_1': 'offline', 'OTHER': 'offline'}}}},
            'pandaserver01': {'record': {
                'queue_overrides': {'status': {'BNL_OSG_PanDA_1': None, 'UM_GREX_PanDA_1': None,
                                               'BNL_OSG_EPIC_PROD_1': 'brokeroff',
                                               'JLAB_PanDA_1': 'brokeroff'}},
                'jedi_regulation': {'pools': {'wlcg': {'osg': {'members': ['BNL_OSG_PanDA_1']}}}}}},
        }
        self.assertEqual(registry.drift(ok, record), [])
        bad = copy.deepcopy(ok)
        bad['osgsub01-harvester']['record']['queue_status']['status']['BNL_OSG_EPIC_PROD_1'] = 'online'
        bad['osgsub01-harvester']['record']['queue_status']['status']['BNL_OSG_PanDA_1'] = 'offline'
        bad['pandaserver01']['record']['queue_overrides']['status'].pop('BNL_OSG_EPIC_PROD_1')
        bad['pandaserver01']['record']['jedi_regulation']['pools']['wlcg']['osg']['members'].append(
            'BNL_OSG_EPIC_PROD_1')
        found = {(d['queue'], d['copy'].split(' on ')[0].split(' (')[0]) for d in registry.drift(bad, record)}
        self.assertEqual(found, {
            ('BNL_OSG_EPIC_PROD_1', 'harvester'), ('BNL_OSG_PanDA_1', 'harvester'),
            ('BNL_OSG_EPIC_PROD_1', 'PanDA schedconfig override'),
            ('BNL_OSG_EPIC_PROD_1', 'JEDI throttler pool osg')})


if __name__ == '__main__':
    unittest.main()
