"""The native launcher's deadline: the glidein's death time as the pilot's shutdowntime."""
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'pilot/native/glidein-deadline.sh'


def run(env, here):
    probe = ('set -euo pipefail; HERE=%s; source %s; echo "$DEADLINE_VERDICT|${DEADLINE_LEFT:-}|${DEADLINE_AT:-}|'
             '${MACHINEFEATURES:-}|$EPICPROD_MIN_TIME_FOR_NEW_JOB"' % (here, SCRIPT))
    full = {'PATH': os.environ['PATH']}
    full.update(env)
    out = subprocess.run(['bash', '-c', probe], env=full, capture_output=True, text=True, timeout=10)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip().split('|')


def machine_ad(directory, todie):
    ad = Path(directory) / 'machine.ad'
    ad.write_text('Name = "slot1@x"\nGLIDEIN_Site = "Y"\n' + ('GLIDEIN_ToDie = %d\n' % todie if todie else '') + 'Cpus = 1\n')
    return str(ad)


class DeadlineTests(unittest.TestCase):
    def test_absent_ad_is_unknown_and_sets_no_machinefeatures(self):
        with tempfile.TemporaryDirectory() as d:
            verdict, left, at, mf, minimum = run({'_CONDOR_MACHINE_AD': d + '/nonexistent'}, d)
            self.assertEqual((verdict, left, at, mf, minimum), ('unknown', '', '', '', '9000'))

    def test_ad_without_todie_is_unknown(self):
        with tempfile.TemporaryDirectory() as d:
            verdict, left, at, mf, _ = run({'_CONDOR_MACHINE_AD': machine_ad(d, None)}, d)
            self.assertEqual((verdict, mf), ('unknown', ''))

    def test_long_life_is_ok_and_written(self):
        with tempfile.TemporaryDirectory() as d:
            todie = int(time.time()) + 36000
            verdict, left, at, mf, _ = run({'_CONDOR_MACHINE_AD': machine_ad(d, todie)}, d)
            self.assertEqual(verdict, 'ok')
            self.assertEqual(int(at), todie)
            self.assertEqual(mf, d + '/machinefeatures')
            self.assertEqual((Path(d) / 'machinefeatures/shutdowntime').read_text().strip(), str(todie))
            self.assertTrue(35990 <= int(left) <= 36000)

    def test_short_life_is_short(self):
        with tempfile.TemporaryDirectory() as d:
            verdict, left, _, _, _ = run({'_CONDOR_MACHINE_AD': machine_ad(d, int(time.time()) + 3000)}, d)
            self.assertEqual(verdict, 'short')

    def test_boundary_at_the_minimum_is_ok(self):
        with tempfile.TemporaryDirectory() as d:
            verdict, _, _, _, _ = run({'_CONDOR_MACHINE_AD': machine_ad(d, int(time.time()) + 9000 + 5)}, d)
            self.assertEqual(verdict, 'ok')

    def test_earlier_site_shutdown_wins_and_site_files_are_kept(self):
        with tempfile.TemporaryDirectory() as d:
            site = Path(d) / 'site-features'
            site.mkdir()
            earlier = int(time.time()) + 20000
            (site / 'shutdowntime').write_text('%d\n' % earlier)
            (site / 'hs06').write_text('11.5\n')
            verdict, _, at, mf, _ = run({'_CONDOR_MACHINE_AD': machine_ad(d, earlier + 50000),
                                         'MACHINEFEATURES': str(site)}, d)
            self.assertEqual((verdict, int(at)), ('ok', earlier))
            self.assertEqual(mf, d + '/machinefeatures')
            self.assertEqual((Path(d) / 'machinefeatures/hs06').read_text().strip(), '11.5')
            self.assertEqual((site / 'shutdowntime').read_text().strip(), str(earlier))  # the site's file untouched

    def test_fresh_execution_ignores_a_stale_generated_deadline(self):
        with tempfile.TemporaryDirectory() as d:
            stale = Path(d) / 'machinefeatures'
            stale.mkdir()
            (stale / 'shutdowntime').write_text('%d\n' % (int(time.time()) + 100))
            todie = int(time.time()) + 36000
            verdict, _, at, _, _ = run({'_CONDOR_MACHINE_AD': machine_ad(d, todie), 'MACHINEFEATURES': str(stale)}, d)
            self.assertEqual((verdict, int(at)), ('ok', todie))

    def test_invalid_override_falls_back_to_the_default(self):
        with tempfile.TemporaryDirectory() as d:
            _, _, _, _, minimum = run({'_CONDOR_MACHINE_AD': machine_ad(d, int(time.time()) + 36000),
                                       'EPICPROD_MIN_TIME_FOR_NEW_JOB': 'soon'}, d)
            self.assertEqual(minimum, '9000')
            _, _, _, _, minimum = run({'_CONDOR_MACHINE_AD': machine_ad(d, int(time.time()) + 36000),
                                       'EPICPROD_MIN_TIME_FOR_NEW_JOB': '5400'}, d)
            self.assertEqual(minimum, '5400')


if __name__ == '__main__':
    unittest.main()
