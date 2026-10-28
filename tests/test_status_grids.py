"""Status grids: the event-service samples (gES, pES, gRS, gRU, gOP, pOP),
which no production request asks for, are in no grid; an x-binned family is
one grid of species and beam against x."""
import unittest

from swf_epicprod.analytics.status_grids import (
    ES_VALIDATION_RE, _column_order, _row_order, column_label, split_path)


class XBinnedFamilyTest(unittest.TestCase):
    PATHS = [f'/EVGEN/SIDIS/pythia6-lhapdf/1.1.0/{species}/{beam}/{x}'
             for species, beam in (('ep_noradcor', '9x130'), ('eAu_EPPS16_noradcor', '9x100'),
                                   ('eAg_EPPS16_noradcor', '9x115'))
             for x in ('x_1e-4to1e-3', 'x_1e-3to1e-2', 'x_1e-2to1e-1')]

    def test_one_family_of_species_beam_against_x(self):
        parts = [split_path(p) for p in self.PATHS]
        self.assertEqual({p[1] for p in parts}, {'SIDIS/pythia6-lhapdf/1.1.0'})
        self.assertEqual(parts[0][2:4], ('ep_noradcor 9x130', 'x_1e-4to1e-3'))
        self.assertEqual(parts[0][4], self.PATHS[0])
        columns = sorted({p[3] for p in parts}, key=_column_order)
        self.assertEqual(columns, ['x_1e-4to1e-3', 'x_1e-3to1e-2', 'x_1e-2to1e-1'])
        self.assertEqual(column_label(columns[0]), 'x 1e-4 to 1e-3')
        rows = sorted({p[2] for p in parts}, key=_row_order)
        self.assertEqual(rows, ['eAu_EPPS16_noradcor 9x100', 'eAg_EPPS16_noradcor 9x115',
                                'ep_noradcor 9x130'])

    def test_q2_families_keep_their_species(self):
        self.assertEqual(split_path('/EVGEN/DIS/BeAGLE1.03.02-1.0/eCu/ep/10x130/q2_1to10')[1:4],
                         ('DIS/BeAGLE1.03.02-1.0/eCu/ep', '10x130', 'q2_1to10'))
        self.assertEqual(
            split_path('/EVGEN/SIDIS/pythia6-lhapdf/1.0.0/ep_noradcor/9x130/q2_1to10')[1:4],
            ('SIDIS/pythia6-lhapdf/1.0.0/ep_noradcor', '9x130', 'q2_1to10'))


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
