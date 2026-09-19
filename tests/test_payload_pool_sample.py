"""The pool sample in the payload report (docs/NERSC_PERLMUTTER.md, the
pool sample): read from the path the launch names, else from the working
directory or an ancestor, else absent; an unreadable sample is an error
in the report, never a dropped one."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'swf_epicprod', 'payload'))
import payload_report as pr  # noqa: E402


class PoolSampleTest(unittest.TestCase):
    def test_named_path_wins(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'named.json')
            with open(p, 'w') as f:
                json.dump({'schema': 'pool-sample/1', 'machine': {'pending_jobs': 3}}, f)
            s = pr.pool_sample(p, cwd=d)
            self.assertEqual(s['machine']['pending_jobs'], 3)
            self.assertEqual(s['path'], p)

    def test_ancestor_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, pr.POOL_SAMPLE_NAME), 'w') as f:
                json.dump({'schema': 'pool-sample/1'}, f)
            deep = os.path.join(d, 'PanDA_Pilot-1', '44')
            os.makedirs(deep)
            self.assertEqual(pr.pool_sample('', cwd=deep)['schema'], 'pool-sample/1')
            # Four levels down is beyond the walk.
            deeper = os.path.join(deep, 'a', 'b')
            os.makedirs(deeper)
            self.assertIsNone(pr.pool_sample('', cwd=deeper))

    def test_absent_is_none(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(pr.pool_sample(os.path.join(d, 'missing.json'), cwd=d))

    def test_unreadable_is_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, pr.POOL_SAMPLE_NAME)
            with open(p, 'w') as f:
                f.write('{not json')
            self.assertIn('error', pr.pool_sample(p, cwd=d))
            with open(p, 'w') as f:
                f.write('[1]')
            self.assertIn('error', pr.pool_sample(p, cwd=d))


if __name__ == '__main__':
    unittest.main()
