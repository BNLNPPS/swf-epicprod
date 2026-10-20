"""The node guard's OSG matchmaking clause (scripts/osg-node-guard-clause.py)."""
import datetime
import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout

_PATH = os.path.join(os.path.dirname(__file__), '..', 'scripts', 'osg-node-guard-clause.py')
_spec = importlib.util.spec_from_file_location('osg_node_guard_clause', _PATH)
ngc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ngc)

NOW = datetime.datetime(2026, 10, 20, 18, 0, tzinfo=datetime.timezone.utc)
SDF = '\n'.join([
    'request_memory = 3700',
    'Requirements = (OSGVO_OS_STRING == "RHEL 9") && (MY.EpicprodNodeGuard =!= False)',
    ngc.MARKER,
    '+EpicprodNodeGuard = True',
    'queue 1',
    '',
])


def document(nodes, mode='live', valid_until='2026-10-20T18:10:00.000000+00:00'):
    return {'generated_at': '2026-10-20T17:50:00.000000+00:00', 'valid_until': valid_until,
            'mode': mode, 'nodes': nodes}


def node(host, site, queue='BNL_OSG_PanDA_1', until='2026-10-22T09:19:04.065028+00:00'):
    return {'queue': queue, 'host': host, 'site': site, 'until': until}


class ExcludedNodesTest(unittest.TestCase):
    def test_live_document_names_the_osg_queues_nodes(self):
        doc = document([node('cn-leo-070.cr.cnaf.infn.it', 'CNAF'),
                        node('nid001234', 'NERSC', queue='NERSC_Perlmutter_epic'),
                        node('n358', 'GREX', queue='BNL_OSG_EPIC_PROD_1')])
        self.assertEqual(ngc.excluded_nodes(doc, NOW),
                         [('cn-leo-070.cr.cnaf.infn.it', 'CNAF'), ('n358', 'GREX')])

    def test_shadow_or_expired_document_excludes_nothing(self):
        nodes = [node('cn-leo-070.cr.cnaf.infn.it', 'CNAF')]
        self.assertEqual(ngc.excluded_nodes(document(nodes, mode='shadow'), NOW), [])
        self.assertEqual(ngc.excluded_nodes(document(nodes, valid_until='2026-10-20T17:59:00Z'), NOW), [])

    def test_a_node_past_its_expiry_is_not_excluded(self):
        doc = document([node('cn-leo-070.cr.cnaf.infn.it', 'CNAF', until='2026-10-20T17:00:00+00:00')])
        self.assertEqual(ngc.excluded_nodes(doc, NOW), [])

    def test_a_node_on_two_osg_queues_is_named_once(self):
        doc = document([node('cn-leo-070.cr.cnaf.infn.it', 'CNAF'),
                        node('cn-leo-070.cr.cnaf.infn.it', 'CNAF', queue='BNL_OSG_EPIC_PROD_1')])
        self.assertEqual(len(ngc.excluded_nodes(doc, NOW)), 1)


class ClauseTest(unittest.TestCase):
    def test_no_nodes_is_true(self):
        self.assertEqual(ngc.clause([]), ('True', []))

    def test_full_name_alone_bare_name_with_its_site(self):
        expression, skipped = ngc.clause([('cn-leo-070.cr.cnaf.infn.it', 'CNAF'), ('n358', 'GREX')])
        self.assertEqual(skipped, [])
        self.assertEqual(expression,
                         '!( regexp("^cn-leo-070[.]cr[.]cnaf[.]infn[.]it$", TARGET.Machine) =?= True || '
                         '(TARGET.GLIDEIN_Site =?= "GREX" && regexp("^n358([.]|$)", TARGET.Machine) =?= True) )')

    def test_bare_name_without_site_or_odd_name_is_not_named(self):
        expression, skipped = ngc.clause([('n358', ''), ('bad host"', 'X')])
        self.assertEqual(expression, 'True')
        self.assertEqual(skipped, [('n358', ''), ('bad host"', 'X')])


class RewriteTest(unittest.TestCase):
    def test_only_the_two_lines_change(self):
        new = ngc.rewrite(SDF, '!( x )', '2026-10-20T18:10:00+00:00')
        old_lines, new_lines = SDF.split('\n'), new.split('\n')
        changed = [i for i, (a, b) in enumerate(zip(old_lines, new_lines)) if a != b]
        self.assertEqual(changed, [2, 3])
        self.assertEqual(new_lines[3], '+EpicprodNodeGuard = !( x )')
        self.assertEqual(ngc.written_validity(new).isoformat(), '2026-10-20T18:10:00+00:00')

    def test_no_marker_is_refused(self):
        with self.assertRaises(ValueError):
            ngc.rewrite('queue 1\n', 'True', '2026-10-20T18:10:00+00:00')


class MainTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.sdf = os.path.join(self.dir, 'pull.sdf')
        with open(self.sdf, 'w') as f:
            f.write(SDF)
        os.chmod(self.sdf, 0o644)

    def run_main(self, url):
        out = io.StringIO()
        with redirect_stdout(out):
            status = ngc.main(['osg-node-guard-clause.py', self.sdf, '--url', url])
        with open(self.sdf) as f:
            return status, f.read(), out.getvalue()

    def publish(self, doc):
        path = os.path.join(self.dir, 'node-exclusion.json')
        with open(path, 'w') as f:
            json.dump(doc, f)
        return 'file://' + path

    def test_live_document_is_written_and_mode_kept(self):
        future = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=20)).isoformat()
        url = self.publish(document([node('cn-leo-070.cr.cnaf.infn.it', 'CNAF', until=future)],
                                    valid_until=future))
        status, text, out = self.run_main(url)
        self.assertEqual(status, 0)
        self.assertIn('+EpicprodNodeGuard = !( regexp("^cn-leo-070[.]cr[.]cnaf[.]infn[.]it$"', text)
        self.assertIn('cn-leo-070.cr.cnaf.infn.it', out)
        self.assertEqual(oct(os.stat(self.sdf).st_mode & 0o777), '0o644')

    def test_unreadable_document_keeps_a_valid_clause_then_lapses(self):
        missing = 'file://' + os.path.join(self.dir, 'missing.json')
        later = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=10)).isoformat()
        with open(self.sdf, 'w') as f:
            f.write(ngc.rewrite(SDF, '!( x )', later))
        status, text, out = self.run_main(missing)
        self.assertEqual((status, '+EpicprodNodeGuard = !( x )' in text), (1, True))
        earlier = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=1)).isoformat()
        with open(self.sdf, 'w') as f:
            f.write(ngc.rewrite(SDF, '!( x )', earlier))
        status, text, out = self.run_main(missing)
        self.assertEqual((status, '+EpicprodNodeGuard = True' in text), (1, True))


if __name__ == '__main__':
    unittest.main()
