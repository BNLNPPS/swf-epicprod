"""The EVGEN read base has one value in two places: run.sh's XRDRURL and
XRDRBASE defaults, which the payload streams input from, and
evgen_es.JLAB_READ, which the Event Service input stage copies from."""
import os
import re
import unittest

from swf_epicprod import evgen_es

RUN_SH = os.path.join(os.path.dirname(__file__), '..', 'swf_epicprod', 'payload', 'run.sh')


class EvgenReadBaseTests(unittest.TestCase):
    def test_run_sh_defaults_match_the_input_stage(self):
        text = open(RUN_SH).read()
        url = re.search(r'^XRDRURL=\$\{XRDRURL-"([^"]*)"\}$', text, re.M).group(1)
        base = re.search(r'^XRDRBASE=\$\{XRDRBASE:-"([^"]*)"\}$', text, re.M).group(1)
        self.assertEqual(url.rstrip('/') + '/' + base, evgen_es.JLAB_READ)
        self.assertEqual(base, '/jlab-osdf-ro/eic/EPIC/cache')


if __name__ == '__main__':
    unittest.main()
