"""Removing datasets from a disk RSE (swf_epicprod.rse_deletion): the
request guard, the tape-copy gate, and the live re-check at execution."""
import unittest

from swf_epicprod import rse_deletion as rd


def _rule(rse, state='OK', ok=10, replicating=0, stuck=0, rid=None):
    return {'id': rid or f'{rse}-rule', 'rse_expression': rse, 'state': state,
            'locks_ok_cnt': ok, 'locks_replicating_cnt': replicating,
            'locks_stuck_cnt': stuck}


class FakeClient:
    def __init__(self, datasets):
        # {name: (length, [rules])}
        self.datasets = datasets
        self.updated = []

    def list_dids(self, scope, filters, did_type=None):
        import fnmatch
        return [n for n in self.datasets if fnmatch.fnmatchcase(n, filters['name'])]

    def get_did(self, scope, name, dynamic_depth=None):
        assert dynamic_depth == 'FILE'
        return {'length': self.datasets[name][0], 'bytes': self.datasets[name][0] * 100,
                'open': True}

    def list_did_rules(self, scope, name):
        return list(self.datasets[name][1])

    def update_replication_rule(self, rule_id, options):
        self.updated.append((rule_id, options))


class ValidateTest(unittest.TestCase):
    def test_refuses_tape_unknown_rse_and_broad_patterns(self):
        for rse, pattern in (('JLAB-TAPE-SE', '/RECO/26.03.0/*'),
                             ('ASGC-XRD', '/RECO/26.03.0/*'),
                             ('BNL-XRD', 'RECO/26.03.0/*'),
                             ('BNL-XRD', '/RECO/*'),
                             ('BNL-XRD', '/*/26.03.0/*')):
            with self.assertRaises(rd.RseDeletionError, msg=(rse, pattern)):
                rd.validate(rse, pattern)

    def test_accepts_campaign_pattern(self):
        self.assertEqual(rd.validate(' BNL-XRD ', '/RECO/26.03.0/*'),
                         ('BNL-XRD', '/RECO/26.03.0/*'))


class TapeCopyTest(unittest.TestCase):
    def test_complete_needs_ok_rule_covering_every_file(self):
        self.assertTrue(rd.tape_copy([_rule('JLAB-TAPE-SE', ok=10)], 10)[0])
        for rules in ([], [_rule('JLAB-TAPE-SE', state='SUSPENDED', ok=0, stuck=10)],
                      [_rule('JLAB-TAPE-SE', ok=9)],
                      [_rule('JLAB-TAPE-SE', state='REPLICATING', ok=9, replicating=1)],
                      [_rule('JLAB-TAPE-SE-OTHER', ok=10)]):
            self.assertFalse(rd.tape_copy(rules, 10)[0], rules)

    def test_empty_dataset_is_never_complete(self):
        self.assertFalse(rd.tape_copy([_rule('JLAB-TAPE-SE', ok=0)], 0)[0])


def final(names):
    return {n: 'final' for n in names}


class PlanExecuteTest(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient({
            '/RECO/26.03.0/a': (10, [_rule('BNL-XRD', rid='a-bnl'), _rule('JLAB-TAPE-SE')]),
            '/RECO/26.03.0/b': (10, [_rule('BNL-XRD', rid='b-bnl'),
                                     _rule('JLAB-TAPE-SE', state='SUSPENDED', ok=0, stuck=10)]),
            '/RECO/26.03.0/c': (10, [_rule('JLAB-TAPE-SE')]),
            '/RECO/26.04.1/d': (10, [_rule('BNL-XRD', rid='d-bnl'), _rule('JLAB-TAPE-SE')]),
        })

    def test_plan_marks_only_tape_complete_datasets_with_a_rule(self):
        p = rd.plan(self.client, 'BNL-XRD', '/RECO/26.03.0/*', task_states=final)
        by = {i['name']: i for i in p['datasets']}
        self.assertEqual(set(by), {'/RECO/26.03.0/a', '/RECO/26.03.0/b', '/RECO/26.03.0/c'})
        self.assertTrue(by['/RECO/26.03.0/a']['eligible'])
        self.assertIn('tape copy incomplete', by['/RECO/26.03.0/b']['reason'])
        self.assertIn('no rule on BNL-XRD', by['/RECO/26.03.0/c']['reason'])
        self.assertEqual(p['totals']['eligible'], 1)
        self.assertEqual(self.client.updated, [])

    def test_execute_rechecks_and_expires_only_what_still_passes(self):
        # a lost its tape copy after the plan; d does not match the pattern.
        self.client.datasets['/RECO/26.03.0/e'] = (
            10, [_rule('BNL-XRD', rid='e-bnl'), _rule('JLAB-TAPE-SE')])
        self.client.datasets['/RECO/26.03.0/a'] = (
            10, [_rule('BNL-XRD', rid='a-bnl'), _rule('JLAB-TAPE-SE', ok=8)])
        seen = []
        r = rd.execute(self.client, 'BNL-XRD', '/RECO/26.03.0/*',
                       ['/RECO/26.03.0/a', '/RECO/26.03.0/e', '/RECO/26.04.1/d'],
                       task_states=final, on_outcome=seen.append)
        self.assertEqual([o['name'] for o in seen],
                         ['/RECO/26.03.0/a', '/RECO/26.03.0/e', '/RECO/26.04.1/d'])
        outcomes = {o['name']: o['outcome'] for o in r['datasets']}
        self.assertEqual(outcomes, {'/RECO/26.03.0/a': 'skipped',
                                    '/RECO/26.03.0/e': 'removed',
                                    '/RECO/26.04.1/d': 'skipped'})
        self.assertEqual(self.client.updated, [('e-bnl', {'lifetime': 0})])
        self.assertEqual(r['totals']['removed'], 1)


    def test_active_or_unread_task_keeps_a_tape_complete_dataset(self):
        p = rd.plan(self.client, 'BNL-XRD', '/RECO/26.03.0/a',
                    task_states=lambda names: {n: 'active' for n in names})
        self.assertIn('still active', p['datasets'][0]['reason'])
        p = rd.plan(self.client, 'BNL-XRD', '/RECO/26.03.0/a')
        self.assertIn('not read', p['datasets'][0]['reason'])
        p = rd.plan(self.client, 'BNL-XRD', '/RECO/26.03.0/a',
                    task_states=lambda names: {})
        self.assertTrue(p['datasets'][0]['eligible'], 'no recorded task')
        r = rd.execute(self.client, 'BNL-XRD', '/RECO/26.03.0/*', ['/RECO/26.03.0/a'],
                       task_states=lambda names: {n: 'active' for n in names})
        self.assertEqual(r['totals']['skipped'], 1)
        self.assertEqual(self.client.updated, [])


if __name__ == '__main__':
    unittest.main()
