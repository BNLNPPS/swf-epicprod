"""The requested-event reading (swf_epicprod/request_events.py):
every shape of answer the production request form has actually received,
and what the parser makes of it. The threshold discussion rests on these
readings, so each one is a test."""
import unittest

from swf_epicprod import request_events as req

M, K, B = 1e6, 1e3, 1e9


class Reading(unittest.TestCase):
    def check(self, answer, expected, note_contains=None):
        events, note = req.parse_events(answer)
        self.assertEqual(events, expected, f'{answer!r} -> {events} ({note})')
        if note_contains:
            self.assertIn(note_contains, note, f'{answer!r}: {note}')

    def test_a_plain_count(self):
        self.check('532904', 532904)
        self.check('200000 events', 200000)

    def test_units(self):
        self.check('10M', 10 * M)
        self.check('10Million', 10 * M)
        self.check('1 Million', 1 * M)
        self.check('1 mil', 1 * M)
        self.check('2.4 million', 2.4 * M)
        self.check('650k', 650 * K)
        self.check('10K', 10 * K)

    def test_scientific_notation_is_one_number(self):
        """'4.00E+06' must not split on its own plus sign."""
        self.check('4.00E+06', 4 * M)
        self.check('1.00E+10', 10 * B)

    def test_a_thousands_comma_is_not_a_separator(self):
        self.check('~900,000 (see comment on priority in document from Kong)', 900 * K)

    def test_a_list_separator_is(self):
        self.check('50K, 50K, 100K, 100K', 300 * K, 'summed')
        self.check('40M,40M,10M,5M (for the 4 Q2 bins), all existing', 95 * M, 'summed')
        self.check('5Mil + 5Mil (w/ & w/o radiative correction )', 10 * M, 'summed')

    def test_a_multiplication(self):
        self.check('5M x 3', 15 * M, 'multiplied')
        self.check('2000000 x 4', 8 * M, 'multiplied')
        self.check('6 x 300k', 1.8 * M, 'multiplied')
        self.check('12 x 1.1m', 13.2 * M, 'multiplied')
        self.check('1000000*6', 6 * M, 'multiplied')

    def test_beam_energies_are_not_counts_or_multipliers(self):
        """'9x130' is a beam energy pair and '5X3X2' a sample grid."""
        self.check('9x130: 1M per file (12M total), 9x275: 2.5M per file (30M total)',
                   42 * M, 'stated total')
        self.check('200k events per sample, 5X3X2 samples altogether, 6M events',
                   6 * M, 'stated total')
        events, _ = req.parse_events('1M 5x41 hel. minus; 1M 5x41 hel. plus; '
                                     '1.3M 10x100 hel. minus; 1.3M 10x100 hel. plus; '
                                     '1.3M 10x130 hel. minus; 1.3M 10x130 hel. plus; '
                                     '1M 10x250 hel. minus; 1M 10x250 hel. plus; '
                                     '1M 18x275 hel. minus; 1M 18x275 hel. plus')
        self.assertAlmostEqual(events, 11.2 * M, places=0)

    def test_a_stated_total_wins_over_its_parts(self):
        self.check('total of 15M', 15 * M, 'stated total')
        self.check('5M in each Q2 range at each energy, so 20M in total', 20 * M,
                   'stated total')

    def test_per_range_without_a_total_is_a_lower_bound(self):
        self.check('1M in each energy range', 1 * M, 'lower bound')
        self.check('1m events in each energy range', 1 * M, 'lower bound')

    def test_prose_around_the_number(self):
        self.check("1M without background.  Could be less with background.  "
                   "I'm not set up", 1 * M)

    def test_an_answer_with_no_number(self):
        self.assertEqual(req.parse_events(''), (None, 'no answer'))
        self.assertEqual(req.parse_events(None), (None, 'no answer'))
        self.assertEqual(req.parse_events('as many as possible')[0], None)


class Percentiles(unittest.TestCase):
    def test_interpolates_between_the_ordered_values(self):
        got = req.percentiles([1, 2, 3, 4], (50,))
        self.assertAlmostEqual(got[50], 2.5)

    def test_an_empty_population_has_none(self):
        self.assertEqual(req.percentiles([], (50,)), {50: None})


class Human(unittest.TestCase):
    def test_reads_as_a_person_writes_it(self):
        self.assertEqual(req.human(10 * M), '10M')
        self.assertEqual(req.human(1.8 * M), '1.8M')
        self.assertEqual(req.human(650 * K), '650k')
        self.assertEqual(req.human(10 * B), '10B')
        self.assertEqual(req.human(None), '-')


if __name__ == '__main__':
    unittest.main()
