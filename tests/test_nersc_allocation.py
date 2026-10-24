"""The NERSC allocation balance, over a fake IRI answer
(swf_epicprod.nersc_allocation.summarize and line are pure)."""
import unittest

from swf_epicprod.nersc_allocation import line, summarize, queue_switches

PROJECTS = [{'id': 'p1', 'name': 'm2616', 'description': 'ATLAS'},
            {'id': 'p2', 'name': 'm3763', 'description': 'ePIC'}]
CAP = 'https://api.iri.nersc.gov/api/v1/account/capabilities/'
ALLOCATIONS = {'p2': [
    {'entries': [{'allocation': 2.2e13, 'usage': 1.8e9, 'unit': 'bytes'}],
     'capability_uri': CAP + 'gpfs_storage'},
    {'entries': [{'allocation': 3075.305, 'usage': 32.248, 'unit': 'node_hours'}],
     'capability_uri': CAP + 'cpu'},
    {'entries': [{'allocation': 1526.315, 'usage': 350.826, 'unit': 'node_hours'}],
     'capability_uri': CAP + 'gpu'}]}


class Balance(unittest.TestCase):

    def test_commissioning_is_preserved_while_production_reserve_is_enforced(self):
        record = {'projects': {'m3763': {'cpu': {'allocation': 100, 'usage': 93}}}}
        prod, es = 'NERSC_Perlmutter_epic', 'NERSC_Perlmutter_epic_es'
        queues = {prod: 'm3763', es: 'm3763'}
        changes = queue_switches(record, queues, 0.9, {}, commissioning={es})
        self.assertEqual([(c['queue'], c['action']) for c in changes], [(prod, 'close')])
        changes = queue_switches(record, queues, 0.9, {})
        self.assertEqual({c['queue'] for c in changes}, {prod, es})

    def test_balance_recovery_does_not_promote_a_commissioning_queue(self):
        record = {'projects': {'m3763': {'cpu': {'allocation': 100, 'usage': 50}}}}
        es = 'NERSC_Perlmutter_epic_es'
        changes = queue_switches(record, {es: 'm3763'}, 0.9,
                                 {es: 'NERSC allocation: m3763 CPU 90% or more used'},
                                 commissioning={es})
        self.assertEqual(changes, [])

    def test_node_hours_per_capability(self):
        out = summarize(PROJECTS, ALLOCATIONS, ['m3763'])
        self.assertEqual(out['m3763']['cpu'],
                         {'allocation': 3075.3, 'usage': 32.2, 'remaining': 3043.1})
        self.assertEqual(out['m3763']['gpu']['remaining'], 1175.5)
        self.assertNotIn('gpfs_storage', out['m3763'])

    def test_project_not_visible(self):
        out = summarize(PROJECTS[:1], {}, ['m3763'])
        self.assertEqual(out, {'m3763': {'visible': False}})
        record = {'projects': out, 'visible_projects': ['m2616', 'm5037'], 'error': ''}
        self.assertEqual(line(record),
                         ['m3763: not visible to the token (it sees m2616, m5037)'])

    def test_lines(self):
        record = {'projects': summarize(PROJECTS, ALLOCATIONS, ['m3763']), 'error': ''}
        self.assertEqual(line(record), ['m3763: CPU 3,043 of 3,075 node-hours left; '
                                        'GPU 1,176 of 1,526 node-hours left'])
        self.assertEqual(line({}), ['not read yet'])
        self.assertEqual(line({'error': 'IRI API HTTP 401: Unauthorized'}),
                         ['unreadable: IRI API HTTP 401: Unauthorized'])


if __name__ == '__main__':
    unittest.main()
