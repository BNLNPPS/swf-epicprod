"""Status grids: production samples (gES, pES, gRS, gRU, gOP, pOP, cES, any
discriminator), which no production request asks for, are in no grid; an x-binned family is
one grid of species and beam against x."""
import unittest

from swf_epicprod.analytics.status_grids import (
    _column_order, _row_order, column_label, family_stem, is_production_sample, latest_delivery,
    merge_variants, sample_name, split_path, under_sample_prefix)


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


class SiblingFamilyTest(unittest.TestCase):
    """Families under one generator version are one grid, the segments
    that differ ahead of the beam in the row."""

    @staticmethod
    def cells(paths):
        out = []
        for path in paths:
            area, family, beam, q2, _ = split_path(path)
            out.append({'area': area, 'family': family, 'beam': beam, 'q2': q2})
        merge_variants(out)
        return out

    def test_stem_ends_at_the_generator_version(self):
        self.assertEqual(family_stem('EXCLUSIVE/DDVCS_ABCONV/EpIC1.1.6-1.0/edecay/hminus'),
                         ('EXCLUSIVE/DDVCS_ABCONV/EpIC1.1.6-1.0', ['edecay', 'hminus']))
        self.assertEqual(family_stem('DIS/NC'), ('DIS/NC', []))
        self.assertEqual(family_stem('DIS/x/1.0.0/ep, with background Bkg_a/1.2'),
                         ('DIS/x/1.0.0, with background Bkg_a/1.2', ['ep']))

    def test_ddvcs_channels_are_one_grid(self):
        cells = self.cells(
            f'/EVGEN/EXCLUSIVE/DDVCS_ABCONV/EpIC1.1.6-1.0/{d}/{h}/{b}/q2_0_10'
            for d in ('edecay', 'mudecay') for h in ('hminus', 'hplus') for b in ('10x100', '18x275'))
        self.assertEqual({c['family'] for c in cells}, {'EXCLUSIVE/DDVCS_ABCONV/EpIC1.1.6-1.0'})
        rows = sorted({c['beam'] for c in cells}, key=_row_order)
        self.assertEqual(rows[:4], ['edecay/hminus 10x100', 'edecay/hplus 10x100',
                                    'mudecay/hminus 10x100', 'mudecay/hplus 10x100'])
        self.assertEqual(len(rows), 8)

    def test_shared_variant_segments_stay_in_the_family(self):
        cells = self.cells(['/EVGEN/DIS/DJANGOH4.6.10-4.0/CC/Rad/eMinus-pMinus/9x275/q2_1to10',
                            '/EVGEN/DIS/DJANGOH4.6.10-4.0/CC/Rad/eMinus-pPlus/9x275/q2_1to10'])
        self.assertEqual({c['family'] for c in cells}, {'DIS/DJANGOH4.6.10-4.0/CC/Rad'})
        self.assertEqual({c['beam'] for c in cells}, {'eMinus-pMinus 9x275', 'eMinus-pPlus 9x275'})

    def test_a_lone_family_and_other_versions_are_left_alone(self):
        cells = self.cells(['/EVGEN/DIS/BeAGLE1.03.02-4.0/eAg/9x115/q2_1to10',
                            '/EVGEN/DIS/BeAGLE1.03.02-5.0/eAg/9x115/q2_1to10'])
        self.assertEqual([(c['family'], c['beam']) for c in cells],
                         [('DIS/BeAGLE1.03.02-4.0/eAg', '9x115'),
                          ('DIS/BeAGLE1.03.02-5.0/eAg', '9x115')])

    def test_x_binned_family_is_not_merged_with_q2_families(self):
        cells = self.cells(XBinnedFamilyTest.PATHS[:1]
                           + ['/EVGEN/SIDIS/pythia6-lhapdf/1.1.0/ep_noradcor/9x130/q2_1to10',
                              '/EVGEN/SIDIS/pythia6-lhapdf/1.1.0/eAu_EPPS16_noradcor/9x100/q2_1to10'])
        self.assertEqual(cells[0]['family'], 'SIDIS/pythia6-lhapdf/1.1.0')
        self.assertEqual(cells[0]['beam'], 'ep_noradcor 9x130')
        self.assertEqual({c['beam'] for c in cells[1:]},
                         {'ep_noradcor 9x130', 'eAu_EPPS16_noradcor 9x100'})
        self.assertTrue(all(c['variant'] for c in cells[1:]))
        self.assertNotIn('variant', cells[0])

    def test_single_particles_keep_their_families(self):
        cells = self.cells(['/EVGEN/SINGLE/e+/1.0/100MeV/3to50deg',
                            '/EVGEN/SINGLE/e-/1.0/100MeV/3to50deg'])
        self.assertEqual(len({c['family'] for c in cells}), 2)


class ProductionSampleTest(unittest.TestCase):
    @staticmethod
    def sample(name):
        # The record pcs.production_samples.compose writes.
        return {'production_sample': {'sample': name, 'events': 100,
                                      'source': 'group.EIC.26.07.3.epic_craterlake.p1107.e37.s11.r11.3to50deg'}}

    def test_production_samples_are_left_out_whatever_their_discriminator(self):
        for name in ('gES1', 'pES12', 'gRS2', 'gRU1', 'gOP000001', 'pOP000011',
                     'cES000001', 'cES000002'):
            self.assertTrue(is_production_sample(self.sample(name)), name)

    def test_campaign_editions_are_kept(self):
        for metadata in (None, {}, {'stage': 'evgen'}, {'trial': 1},
                         {'production_sample': {}}):
            self.assertFalse(is_production_sample(metadata), metadata)

    def test_paths_under_a_sample_prefix_are_the_samples(self):
        samples = {'cES000001', 'cES000002', 'gOP000001'}
        for path in ('/EVGEN/cES000002/try8/SINGLE/neutron/5GeV/3to50deg.cES000002',
                     '/EVGEN/cES000002/try5/SINGLE/neutron/5GeV/3to50deg.cES000002',
                     '/EVGEN/cES000001/SINGLE/neutron/5GeV/3to50deg.cES000001',
                     '/EVGEN/gOP000001/SINGLE/neutron/5GeV/3to50deg.gOP000001'):
            self.assertTrue(under_sample_prefix(path, samples), path)
        for path in ('/EVGEN/SINGLE/neutron/5GeV/3to50deg',
                     '/EVGEN/Bkg_Exact1S_2us_e_only/GoldCt/10um/DIS/NC/18x275/q2_1',
                     '/EVGEN/cES000003/SINGLE/neutron/5GeV/3to50deg.cES000003',
                     '/EVGEN', ''):
            self.assertFalse(under_sample_prefix(path, samples), path)

    def test_sample_name_is_the_discriminator(self):
        self.assertEqual(sample_name(ProductionSampleTest.sample('cES000002')), 'cES000002')
        self.assertEqual(sample_name({}), '')


class LatestDeliveryTest(unittest.TestCase):
    @staticmethod
    def edition(edition, events, withdrawn=False):
        return {'edition': edition, 'events': events, 'files': 1, 'withdrawn': withdrawn}

    def test_latest_edition_not_the_sum(self):
        row = {'delivered_events': 3000, 'delivered_editions': [
            self.edition('26.07.1', 2000), self.edition('26.07.3', 1000)]}
        self.assertEqual(latest_delivery(row), ('26.07.3', 1000))

    def test_editions_order_by_number(self):
        row = {'delivered_editions': [self.edition('26.07.10', 1), self.edition('26.07.3', 2)]}
        self.assertEqual(latest_delivery(row), ('26.07.10', 1))

    def test_edition_without_events_or_withdrawn_is_passed_over(self):
        row = {'delivered_editions': [self.edition('26.07.1', 2000),
                                      self.edition('26.07.2', 500, withdrawn=True),
                                      self.edition('26.07.3', 0)]}
        self.assertEqual(latest_delivery(row), ('26.07.1', 2000))

    def test_only_withdrawn_delivery_reads_nothing(self):
        row = {'delivered_events': 0,
               'delivered_editions': [self.edition('26.07.2', 500, withdrawn=True)]}
        self.assertEqual(latest_delivery(row), (None, 0))

    def test_record_without_the_split_reads_the_total(self):
        self.assertEqual(latest_delivery({'delivered_events': 700}), (None, 700))
        self.assertEqual(latest_delivery({}), (None, 0))


if __name__ == '__main__':
    unittest.main()
