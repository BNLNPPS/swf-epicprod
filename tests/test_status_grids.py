"""Status grids: the event-service samples (gES, pES, gRS, gRU, gOP, pOP),
which no production request asks for, are in no grid."""
import unittest

from swf_epicprod.analytics.status_grids import ES_VALIDATION_RE


class EsSampleTest(unittest.TestCase):
    def test_es_samples_match(self):
        for sample in ('3to50deg.gES1', '3to50deg.gES3', '3to50deg.pES6', '45to135deg.pES12',
                       '3to50deg.gRS2', '3to50deg.gRU1',
                       '3to50deg.gOP000001', '3to50deg.pOP000011'):
            self.assertTrue(ES_VALIDATION_RE.search(sample), sample)

    def test_campaign_samples_do_not_match(self):
        for sample in ('', '3to50deg', '130to177deg', 'eta0.5', '3to50deg.trial',
                       'EIC_ESR_Xsuite.dataprod_rel_1.0.1.18x275.10000Ahr.MachineRuntime50s'):
            self.assertFalse(ES_VALIDATION_RE.search(sample), sample)


if __name__ == '__main__':
    unittest.main()
