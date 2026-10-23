"""The denominator rule: a PC's target among its edition heads (completion.pc_targets)."""
import unittest
from types import SimpleNamespace

from swf_epicprod.analytics.completion import pc_targets


def head(label, expected, source, pc_id=1):
    return SimpleNamespace(physics_config_id=pc_id,
                           physics_config=SimpleNamespace(label=label),
                           expected_events=expected,
                           expected_events_source=source)


class PcTargetsTests(unittest.TestCase):
    def test_included_on_a_later_edition_outranks_derived_on_an_earlier(self):
        heads = [head('pc6498', None, ''), head('pc6498', 500000, 'derived'),
                 head('pc6498', 5000000, 'included')]
        self.assertEqual(pc_targets(heads), {'pc6498': (5000000, 'included')})

    def test_requested_outranks_derived_and_yields_to_included(self):
        self.assertEqual(pc_targets([head('a', 1, 'derived'), head('a', 2, 'requested')]),
                         {'a': (2, 'requested')})
        self.assertEqual(pc_targets([head('a', 2, 'requested'), head('a', 3, 'included')]),
                         {'a': (3, 'included')})

    def test_among_equal_sources_the_first_edition_stands(self):
        self.assertEqual(pc_targets([head('a', 1, 'included'), head('a', 2, 'included')]),
                         {'a': (1, 'included')})

    def test_targetless_editions_keep_the_targeted_one(self):
        self.assertEqual(pc_targets([head('a', 7, 'derived'), head('a', None, '')]),
                         {'a': (7, 'derived')})

    def test_unknown_source_ranks_last_and_no_target_gives_no_entry(self):
        self.assertEqual(pc_targets([head('a', 9, 'legacy'), head('a', 4, 'derived')]),
                         {'a': (4, 'derived')})
        self.assertEqual(pc_targets([head('a', None, ''), head('b', None, '', pc_id=None)]), {})


if __name__ == '__main__':
    unittest.main()
