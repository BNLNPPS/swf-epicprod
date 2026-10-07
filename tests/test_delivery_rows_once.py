"""Each manifest row counted once in the delivery record
(swf_epicprod.analytics.delivery_daily.count_each_row_once), keyed by
pcs.commands.reco_row_key."""
import datetime as dt
import unittest

from pcs.commands import reco_row_key
from swf_epicprod.analytics.delivery_daily import count_each_row_once

BASE = '/RECO/26.07.1/epic_craterlake/EXCLUSIVE/UPSILON/1S/9x275/hiDiv'
TRY2 = '/RECO/26.07.1/epic_craterlake/try2/EXCLUSIVE/UPSILON/1S/9x275/hiDiv'


def t(hour):
    return dt.datetime(2026, 9, 21, hour, tzinfo=dt.timezone.utc)


def f(location, chunk, hour, nbytes=100):
    return (f'{location}/upsilon_run000.{chunk:04d}.eicrecon.edm4eic.root',
            '26.07', t(hour), nbytes)


class RowsOnce(unittest.TestCase):

    def test_key_is_the_same_across_tries(self):
        self.assertEqual(reco_row_key(f(BASE, 7, 1)[0]), reco_row_key(f(TRY2, 7, 1)[0]))
        self.assertEqual(reco_row_key(f(BASE, 7, 1)[0]),
                         ('26.07.1', 'epic_craterlake', 'EXCLUSIVE/UPSILON/1S/9x275/hiDiv',
                          'upsilon_run000', '0007'))
        self.assertIsNone(reco_row_key('/SIMU/26.07.1/epic_craterlake/x/y.0001.edm4hep.root'))

    def test_earliest_copy_stands(self):
        records = [f(BASE, 1, 9), f(TRY2, 1, 3), f(BASE, 2, 9), f(TRY2, 3, 4)]
        kept, dup = count_each_row_once(records)
        self.assertEqual(sorted(r[0] for r in kept),
                         sorted([f(TRY2, 1, 3)[0], f(BASE, 2, 9)[0], f(TRY2, 3, 4)[0]]))
        self.assertEqual(dup, {BASE: 1})

    def test_versions_are_separate_deliveries(self):
        other = BASE.replace('26.07.1', '26.07.3')
        kept, dup = count_each_row_once([f(BASE, 1, 1), f(other, 1, 2)])
        self.assertEqual((len(kept), dup), (2, {}))

    def test_non_reco_files_pass(self):
        simu = ('/SIMU/26.07.1/epic_craterlake/x/y.0001.edm4hep.root', '26.07', t(1), 5)
        kept, dup = count_each_row_once([simu, simu])
        self.assertEqual((len(kept), dup), (2, {}))


if __name__ == '__main__':
    unittest.main()
