"""The ePIC job throttler's decision over fake per-site statistics
(swf_epicprod.jedi.epic_job_throttler.decide is pure)."""
import unittest

from swf_epicprod.jedi.epic_job_throttler import (
    DEFAULT_NQUEUELIMIT,
    PASS_MAX_JOBS,
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
        d = decide([
            site("OSG", running=1000, not_run=2500, nqueuelimit=100),
            site("GREX", running=100, not_run=50, nqueuelimit=100),
        ])
        self.assertFalse(d.throttled)
        self.assertEqual(d.excluded_sites, ["OSG"])
        self.assertEqual(d.max_num_jobs, 150)  # GREX: max(200, 100) - 50

    def test_caps(self):
        d = decide([site("A", running=50, not_run=10, nrunningcap=40)])
        self.assertTrue(d.throttled)
        d = decide([site("A", running=50, not_run=60, nqueuecap=55)])
        self.assertTrue(d.throttled)

    def test_lack_of_jobs_below_the_floor(self):
        d = decide([site("A", running=10, not_run=10, nqueuelimit=1000)])
        self.assertTrue(d.lack_of_jobs)
        d = decide([site("A", running=10, not_run=950, nqueuelimit=1000)])
        self.assertFalse(d.lack_of_jobs)


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
