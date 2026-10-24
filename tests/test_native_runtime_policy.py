"""Admission at both the first-job decision and the pilot's exported floor."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
NATIVE = ROOT / 'pilot/native'
spec = importlib.util.spec_from_file_location('runtime_policy', NATIVE / 'runtime-policy.py')
policy_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy_module)
POLICY = json.loads((NATIVE / 'queue-runtime-policy.json').read_text())
cohort_spec = importlib.util.spec_from_file_location('native_cohort', ROOT / 'scripts/submit-native-cohort.py')
cohort = importlib.util.module_from_spec(cohort_spec)
cohort_spec.loader.exec_module(cohort)
render_spec = importlib.util.spec_from_file_location('native_render', ROOT / 'scripts/render-native-production.py')
renderer = importlib.util.module_from_spec(render_spec)
render_spec.loader.exec_module(renderer)


class RuntimePolicyTests(unittest.TestCase):
    def test_pilot_maximum_is_site_specific_and_preserves_other_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / 'source.json', Path(directory) / 'queuedata.json'
            source.write_text(json.dumps({'panda_queue': 'BNL_OSG_PanDA_1', 'maxtime': 57600,
                                          'timefloor': 120, 'maxrss': 4096}))
            for site, expected in [('OSG_US_FSU_HNPGRID', 57600), ('SU-ITS', 36000),
                                   ('ComputeCanada-Fir', 36000), ('', 36000)]:
                policy_module.prepare_queuedata(POLICY, 'BNL_OSG_PanDA_1', site, str(source), str(output))
                record = json.loads(output.read_text())
                self.assertEqual(record, {'panda_queue': 'BNL_OSG_PanDA_1', 'maxtime': expected,
                                          'timefloor': 120, 'maxrss': 4096})
            source.write_text('{"panda_queue":"different"}')
            with self.assertRaises(ValueError):
                policy_module.prepare_queuedata(POLICY, 'BNL_OSG_PanDA_1', 'SU-ITS', str(source), str(output))
            self.assertEqual(policy_module.prepare_queuedata(POLICY, 'UM_GREX_PanDA_1', '', str(source), str(output)), '')

    def test_native_retarget_keeps_runtime_image_and_exclusions(self):
        files = ('pilot3-native.tar.gz', 'certificates.tar.gz', 'rucio.cfg',
                 'glidein-deadline.sh', 'job-user.sh')
        unchanged = ('Requirements = (Microarch =!= "x86_64-v2") && (MY.EpicprodNodeGuard =!= False)\n'
                     '+SingularityImage = "/cvmfs/existing-image"\n'
                     'environment = "HARVESTER_ID=existing"\n')
        text = ('executable = /old/epic-native-pilot.sh\n'
                'transfer_input_files = ' + ','.join('/old/' + name for name in files) + '\n'
                '+EpicprodNative = "old"\n+MaxRuntime = {requestWalltimeMinute}\nperiodic_remove = False\n' + unchanged)
        result = renderer.retarget(text, '/new', 'headroom-test')
        self.assertIn('(Microarch =!= "x86_64-v2") && (MY.EpicprodNodeGuard =!= False)', result)
        for line in unchanged.splitlines()[1:]:
            self.assertIn(line, result)
        self.assertIn('/new/runtime-policy.py,/new/queue-runtime-policy.json', result)
        self.assertNotIn('/old/', result)
        self.assertIn('+MaxRuntime = ifThenElse(MachineAttrGLIDEIN_Site0 =?= "OSG_US_FSU_HNPGRID", 960, 600)', result)
        self.assertIn('job_machine_attrs = GLIDEIN_Site', result)
        self.assertEqual(renderer.runtime_limits(result), result)

    def test_matchmaking_floor_uses_the_landing_policy(self):
        gate = policy_module.condor_admission(POLICY, 'BNL_OSG_PanDA_1')
        self.assertIn('TARGET.GLIDEIN_ToDie =?= undefined', gate)
        for site in ('SU-ITS', 'OSG_US_FSU_HNPGRID', 'ComputeCanada-Fir'):
            minimum = policy_module.select(POLICY, 'BNL_OSG_PanDA_1', site)[0]
            self.assertIn('TARGET.GLIDEIN_Site =?= ' + json.dumps(site) + ', ' + str(minimum), gate)
        text = ('Requirements = (GLIDEIN_ToDie =?= undefined || GLIDEIN_ToDie - time() > 7200) && '
                '(MY.EpicprodNodeGuard =!= False)\n+MaxRuntime = 600\nperiodic_remove = False\n')
        result = renderer.runtime_limits(text)
        self.assertIn(gate, result)
        self.assertNotIn('> 7200)', result)
        self.assertEqual(renderer.runtime_limits(result), result)
        with self.assertRaisesRegex(ValueError, 'review existing glidein-life'):
            renderer.runtime_limits(text.replace('> 7200)', '> 8000)'))

    def test_cohort_does_not_silently_reintroduce_the_old_six_hour_budget(self):
        self.assertEqual(cohort.runtime_minutes('+MaxRuntime = 600\n'), 600)
        self.assertEqual(cohort.runtime_minutes('+MaxRuntime = {requestWalltimeMinute}\n', 600), 600)
        with self.assertRaises(ValueError):
            cohort.runtime_minutes('+MaxRuntime = {requestWalltimeMinute}\n')

    def test_queue_and_execution_scope_and_explicit_override(self):
        choose = policy_module.select
        self.assertEqual(choose(POLICY, 'BNL_OSG_PanDA_1', 'SU-ITS')[0], 18900)
        self.assertEqual(choose(POLICY, 'BNL_OSG_PanDA_1', 'OSG_US_FSU_HNPGRID')[0], 24300)
        self.assertEqual(choose(POLICY, 'BNL_OSG_PanDA_1', 'ComputeCanada-Fir')[0], 7800)
        self.assertEqual(choose(POLICY, 'UM_GREX_PanDA_1', 'SU-ITS')[0], 9000)
        self.assertEqual(choose(POLICY, 'BNL_OSG_PanDA_1', 'SU-ITS', '20000')[0], 20000)
        self.assertEqual(choose(POLICY, 'BNL_OSG_PanDA_1', 'SU-ITS', 'soon')[0], 18900)
        custom = {'schema_version': 1, 'default_seconds': 9000,
                  'queues': {'Q': {'minimum_seconds': 12000}}}
        self.assertEqual(choose(custom, 'Q', '')[0], 12000)

    def test_exact_ad_name_keeps_spaces_and_does_not_use_host_aliases(self):
        with tempfile.TemporaryDirectory() as directory:
            ad = Path(directory) / 'machine.ad'
            ad.write_text('Machine = "comphy.fsu.edu"\nGLIDEIN_Site = "UA Little Rock ITS"\n')
            self.assertEqual(policy_module.execution_queue(ad), 'UA Little Rock ITS')
            ad.write_text('Machine = "comphy.fsu.edu"\n')
            self.assertEqual(policy_module.execution_queue(ad), '')

    def test_first_job_declines_short_slow_queue_but_admits_fast_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            here = Path(directory)
            for name in ('runtime-policy.py', 'queue-runtime-policy.json'):
                shutil.copyfile(NATIVE / name, here / name)
            ad = here / 'machine.ad'
            env = {'PATH': os.environ['PATH'], 'PILOT_SITENAME': 'BNL_OSG_PanDA_1',
                   '_CONDOR_MACHINE_AD': str(ad)}
            probe = ('set -euo pipefail; HERE="$1"; source "$2"; '
                     'echo "$DEADLINE_VERDICT|$EPICPROD_MIN_TIME_FOR_NEW_JOB"')
            for site, expected in [('SU-ITS', 'short|18900'), ('ComputeCanada-Fir', 'ok|7800')]:
                ad.write_text(f'GLIDEIN_Site = "{site}"\nGLIDEIN_ToDie = {int(time.time()) + 16000}\n')
                result = subprocess.run(['bash', '-c', probe, 'probe', str(here),
                                         str(NATIVE / 'glidein-deadline.sh')],
                                        env=env, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), expected)
            # A damaged shipped policy must not silently restore the fast floor.
            (here / 'queue-runtime-policy.json').write_text('{invalid')
            result = subprocess.run(['bash', '-c', probe, 'probe', str(here),
                                     str(NATIVE / 'glidein-deadline.sh')],
                                    env=env, capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('no job taken', result.stderr)


if __name__ == '__main__':
    unittest.main()
