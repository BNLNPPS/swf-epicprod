"""The node guard clause script's pure parts."""
import datetime
import importlib.util
from pathlib import Path
import unittest

SPEC = importlib.util.spec_from_file_location(
    'osg_node_guard_clause', Path(__file__).resolve().parents[1] / 'scripts/osg-node-guard-clause.py')
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)

T0 = datetime.datetime(2026, 10, 23, 4, 59, tzinfo=datetime.timezone.utc)


class NodeGuardClauseTests(unittest.TestCase):
    def test_only_pilots_started_after_the_exclusion_are_removed(self):
        before, after = T0 - datetime.timedelta(hours=1), T0 + datetime.timedelta(minutes=5)
        nodes = [('nod46.phys.uconn.edu', 'UConn', T0), ('fc20513', 'ComputeCanada-Fir', None)]
        rows = [('26584', '0', 'nod46.phys.uconn.edu', 'UConn', after),
                ('26585', '1', 'nod47.phys.uconn.edu', 'UConn', after),
                ('26586', '2', 'fc20513.int.fir.alliancecan.ca', 'ComputeCanada-Fir', before),
                ('26587', '3', 'fc20513', 'GREX', after),
                ('26588', '4', 'NOD46.phys.uconn.edu', 'UConn', before),
                ('26589', '5', 'nod46.phys.uconn.edu', 'UConn', None)]
        hits = guard.pilots_on(nodes, rows)
        # 26588 was on nod46 before its exclusion and may be finishing a healthy
        # job; 26586 and 26589 have no known times, and unknown is not evidence
        self.assertEqual([h[0] for h in hits], ['26584'])

    def test_clause_names_full_hosts_and_bare_hosts_with_a_site(self):
        expression, skipped = guard.clause([('nod46.phys.uconn.edu', 'UConn', T0),
                                            ('fc20513', 'ComputeCanada-Fir', None),
                                            ('bare-no-site', '', None)])
        self.assertIn('regexp("^nod46[.]phys[.]uconn[.]edu$", TARGET.Machine)', expression)
        self.assertIn('TARGET.GLIDEIN_Site =?= "ComputeCanada-Fir"', expression)
        self.assertEqual(skipped, [('bare-no-site', '')])

    def test_stamp_names_the_expression(self):
        a, _ = guard.clause([('nod46.phys.uconn.edu', 'UConn', T0)])
        b, _ = guard.clause([('nod47.phys.uconn.edu', 'UConn', T0)])
        self.assertEqual(len(guard.stamp_of(a)), 12)
        self.assertNotEqual(guard.stamp_of(a), guard.stamp_of(b))
        self.assertEqual(guard.stamp_of(a), guard.stamp_of(a))


if __name__ == '__main__':
    unittest.main()
