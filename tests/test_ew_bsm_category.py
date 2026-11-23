"""The electroweak and beyond-the-Standard-Model processes number their
new physics tags in category 7 (EW_BSM), and the tag form offers what the
path reader derives for them."""
from django.test import SimpleTestCase

from pcs import services
from pcs.physics_match import derive_physics
from pcs.schemas import TAG_SCHEMAS


class EwBsmCategoryTests(SimpleTestCase):
    def test_bsm_processes_map_to_category_7(self):
        self.assertEqual(services._PROCESS_CATEGORY['LFV'], 7)
        self.assertEqual(services._PROCESS_CATEGORY['ALP'], 7)

    def test_derived_bsm_processes_have_a_category(self):
        for path in ('EVGEN/EW_BSM/LFV/pythia8.307-1.0/etau/18x275/q2_100to2500',
                     'EVGEN/EW_BSM/ALP/madgraph5-3.7.0-1.0/aem-axem/10x110/ma_1.0'):
            with self.subTest(path=path):
                process = derive_physics(path, beam='')['process']
                self.assertEqual(services._PROCESS_CATEGORY.get(process), 7)

    def test_tag_form_offers_the_lfv_values(self):
        choices = TAG_SCHEMAS['p']['choices']
        self.assertIn('LFV', choices['process'])
        self.assertIn('etau', choices['channel'])
        self.assertIn('q2_100to2500', choices['q2_range'])
