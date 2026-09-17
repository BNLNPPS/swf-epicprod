"""The ePIC job throttler's decision over fake per-site statistics
(swf_epicprod.jedi.epic_job_throttler.decide is pure) and its grant
ledger over a temporary file."""
import os
import tempfile
import unittest

import swf_epicprod.jedi.epic_job_throttler as ejt
from swf_epicprod.jedi.epic_job_throttler import (
    DEFAULT_NQUEUELIMIT,
    PASS_MAX_JOBS,
    GrantLedger,
    SiteReading,
    decide,
    readings_from_stats,
)


def site(name, running=0, not_run=0, defined=0, **limits):
    return SiteReading(site=name, running=running, not_run=not_run, defined=defined, **limits)


class DecideTest(unittest.TestCase):
    def test_no_sites_is_unthrottled_and_uncapped(self):
        d = decide([])
        self.assertFalse(d.throttled)
        self.assertIsNone(d.max_num_jobs)
        self.assertEqual(d.excluded_sites, [])

    def test_one_site_under_its_bound_passes_with_its_room(self):
        # 1000 running, threshold 2: bound 2000; 500 queued leaves 1500, capped per pass
        d = decide([site("OSG", running=1000, not_run=400, defined=100, nqueuelimit=100)])
        self.assertFalse(d.throttled)
        self.assertEqual(d.max_num_jobs, PASS_MAX_JOBS)
        self.assertEqual(d.excluded_sites, [])

    def test_one_site_over_its_bound_throttles(self):
        d = decide([site("OSG", running=1000, not_run=2500, nqueuelimit=100)])
        self.assertTrue(d.throttled)
        self.assertEqual(d.excluded_sites, ["OSG"])

    def test_queued_floor_holds_when_nothing_runs(self):
        # nothing running: the floor is NQUEUELIMIT, not zero
        d = decide([site("GREX", running=0, not_run=DEFAULT_NQUEUELIMIT - 1)])
        self.assertFalse(d.throttled)
        d = decide([site("GREX", running=0, not_run=DEFAULT_NQUEUELIMIT + 1)])
        self.assertTrue(d.throttled)

    def test_saturated_site_is_excluded_while_the_other_passes(self):
        readings = [
            site("OSG", running=1000, not_run=2500, nqueuelimit=100),
            site("GREX", running=100, not_run=50, nqueuelimit=100),
        ]
        d = decide(readings, exclusion_honored=True)
        self.assertFalse(d.throttled)
        self.assertEqual(d.excluded_sites, ["OSG"])
        self.assertEqual(d.max_num_jobs, 150)  # GREX: max(200, 100) - 50
        self.assertEqual(d.granted_sites, ["GREX"])

    def test_saturated_site_throttles_when_the_generator_cannot_exclude(self):
        # 2026-09-17: BNL_PanDA_1's room let the generator fill BNL_OSG_PanDA_1 to 23,000
        d = decide([
            site("BNL_OSG_PanDA_1", running=0, not_run=6621, nqueuelimit=3123),
            site("BNL_PanDA_1", running=30, not_run=0, nqueuelimit=2000),
        ])
        self.assertTrue(d.throttled)
        self.assertEqual(d.excluded_sites, ["BNL_OSG_PanDA_1"])

    def test_grants_count_as_pending_until_the_reading_carries_them(self):
        r = site("OSG", running=0, not_run=748, nqueuelimit=3123)
        r.granted = 2375
        self.assertEqual(r.room, 0)
        d = decide([r])
        self.assertTrue(d.throttled)  # no room: never an uncapped pass
        r.granted = 2000
        d = decide([r])
        self.assertFalse(d.throttled)
        self.assertEqual(d.max_num_jobs, PASS_MAX_JOBS)
        self.assertEqual(d.granted_sites, ["OSG"])

    def test_caps(self):
        d = decide([site("A", running=50, not_run=10, nrunningcap=40)])
        self.assertTrue(d.throttled)
        d = decide([site("A", running=50, not_run=60, nqueuecap=55)])
        self.assertTrue(d.throttled)



class LedgerTest(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self.ledger = GrantLedger(self.path)

    def tearDown(self):
        os.unlink(self.path)

    def test_grants_accumulate_against_one_reading_and_reset_when_it_changes(self):
        readings = [site("OSG", running=0, not_run=748, nqueuelimit=3123)]
        self.ledger.charge(readings)
        self.assertEqual(readings[0].granted, 0)
        self.ledger.grant(readings, ["OSG"], 300)
        self.ledger.grant(readings, ["OSG"], 300)
        again = [site("OSG", running=0, not_run=748, nqueuelimit=3123)]
        self.ledger.charge(again)
        self.assertEqual(again[0].granted, 600)
        moved = [site("OSG", running=0, not_run=2316, nqueuelimit=3123)]
        self.ledger.charge(moved)
        self.assertEqual(moved[0].granted, 0)
        self.ledger.grant(moved, ["OSG"], 100)
        self.ledger.charge(moved)
        self.assertEqual(moved[0].granted, 100)

    def test_grants_age_out(self):
        readings = [site("OSG", running=0, not_run=748, nqueuelimit=3123)]
        self.ledger.grant(readings, ["OSG"], 300)
        ttl = ejt.LEDGER_TTL_S
        try:
            ejt.LEDGER_TTL_S = 0
            self.ledger.charge(readings)
        finally:
            ejt.LEDGER_TTL_S = ttl
        self.assertEqual(readings[0].granted, 0)

    def test_survives_a_garbled_file(self):
        with open(self.path, "w") as fh:
            fh.write("{not json")
        readings = [site("OSG", running=0, not_run=748, nqueuelimit=3123)]
        self.ledger.charge(readings)
        self.ledger.grant(readings, ["OSG"], 300)
        self.ledger.charge(readings)
        self.assertEqual(readings[0].granted, 300)


class ReadingsTest(unittest.TestCase):
    def test_folds_statuses_at_the_resource_type_and_applies_site_limits(self):
        stats = {
            "OSG": {"SCORE": {"running": 5, "activated": 3, "assigned": 1, "starting": 1, "defined": 2},
                    "MCORE": {"running": 99}},
            "GREX": {"MCORE": {"running": 7}},
        }
        r = readings_from_stats(stats, "SCORE", {"OSG": {"THROTTLE_THRESHOLD": 3, "NQUEUELIMIT": 20}})
        self.assertEqual([x.site for x in r], ["OSG"])  # GREX has no SCORE jobs
        osg = r[0]
        self.assertEqual((osg.running, osg.not_run, osg.defined), (5, 5, 2))
        self.assertEqual((osg.threshold, osg.nqueuelimit), (3.0, 20))
        self.assertEqual(osg.bound, 20)  # max(3 x 5, 20)


if __name__ == "__main__":
    unittest.main()
