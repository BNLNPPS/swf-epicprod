"""The detector reference index and its lookup (docs/EPIC_DETECTOR_REFERENCE.md)."""
import importlib.util
import json
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, 'docs', 'epic-detector-reference.json')
spec = importlib.util.spec_from_file_location('pdr_lookup', os.path.join(ROOT, 'scripts', 'pdr-lookup.py'))
lookup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lookup)


class DetectorReferenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(INDEX) as fh:
            cls.index = json.load(fh)

    def test_every_detector_belongs_to_a_system_with_a_section(self):
        self.assertEqual(self.index['unassigned'], [])
        for system in self.index['systems']:
            self.assertTrue(system['section'], system['system'])
            self.assertTrue(system['section']['pdf_page'])

    def test_a_detector_name_leads_to_its_section(self):
        systems, matched = lookup.resolve(self.index, 'drich')
        self.assertEqual(matched, ['DRICH'])
        self.assertEqual(systems[0]['section']['title'], 'The dual radiator RICH')
        parts = [p['title'] for p in systems[0]['section']['parts']]
        self.assertEqual(parts, ['Requirements', 'Justification', 'Implementation'])

    def test_a_prefix_matches_every_station(self):
        systems, matched = lookup.resolve(self.index, 'ForwardRomanPot')
        self.assertEqual(sorted(matched), ['ForwardRomanPot_Station_1', 'ForwardRomanPot_Station_2'])
        self.assertEqual(len(systems), 1)

    def test_a_topic_resolves_when_no_detector_does(self):
        systems, matched = lookup.resolve(self.index, 'radiation')
        self.assertEqual(matched, [])
        self.assertEqual(systems[0]['section']['title'], 'Radiation Level')


if __name__ == '__main__':
    unittest.main()
