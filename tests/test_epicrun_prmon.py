"""The runner's job-level memory monitor (NPPS0_TEST_QUEUE.md, program
step 6): the shape of the in-container run script epicrun writes, over
the spec alone. No container, no prmon, no pilot."""
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'tools', 'worker'))
import epicrun  # noqa: E402

SPEC = {
    'run': 'python payload/run.sh --events 100',
    'outputs': {'out.root': 'RECO/out.root'},
    'env': {'TASKNAME': 'group.EIC.test', 'OUT_RSE': 'BNL-XRD'},
}


class RunScript(unittest.TestCase):
    def setUp(self):
        self.lines = epicrun.run_script_lines(SPEC)
        self.script = "\n".join(self.lines) + "\n"

    def test_the_payload_still_runs_in_workdir(self):
        self.assertEqual(self.lines[1], 'cd workDir || exit 64')

    def test_the_monitor_files_land_in_the_job_directory(self):
        """One level above workDir, where the pilot reads them by name."""
        self.assertIn(f'../{epicrun.PRMON_OUTPUT}', self.script)
        self.assertIn(f'../{epicrun.PRMON_SUMMARY}', self.script)

    def test_the_monitor_watches_the_payload_itself(self):
        """prmon writes its summary when the process it watches exits,
        and on nothing else, so it watches the payload's own subshell."""
        self.assertIn('prmon --pid $__payload', self.script)

    def test_the_payload_runs_in_a_subshell(self):
        run_at = self.lines.index(SPEC['run'])
        self.assertEqual(self.lines[run_at - 1], '(')
        self.assertEqual(self.lines[run_at + 1], ') &')
        self.assertEqual(self.lines[run_at + 2], '__payload=$!')

    def test_a_worker_without_prmon_runs_unwatched(self):
        self.assertIn('if command -v prmon >/dev/null 2>&1; then', self.script)
        self.assertIn('runs unwatched', self.script)

    def test_the_status_is_the_payload_s_own(self):
        """Nothing may stand between waiting on the payload and ec=$?,
        or the job would report the status of whatever did."""
        wait_at = self.lines.index('wait $__payload')
        self.assertEqual(self.lines[wait_at + 1], 'ec=$?')

    def test_the_monitor_is_waited_for_never_killed(self):
        """A killed prmon leaves only the snapshot; one that sees its
        payload end writes the summary the pilot reads."""
        self.assertNotIn('kill', self.script)
        wait_at = next(i for i, line in enumerate(self.lines) if 'wait "$__prmon"' in line)
        status_at = next(i for i, line in enumerate(self.lines)
                         if line.startswith('echo $ec >'))
        self.assertLess(self.lines.index('ec=$?'), wait_at)
        self.assertLess(wait_at, status_at)

    def test_the_environment_is_exported_before_the_payload(self):
        import shlex
        for key, val in SPEC['env'].items():
            export_at = self.lines.index(f"export {key}={shlex.quote(val)}")
            self.assertLess(export_at, self.lines.index(SPEC['run']))

    def test_a_value_with_a_space_is_quoted(self):
        import shlex
        lines = epicrun.run_script_lines(dict(SPEC, env={'NOTE': 'two words'}))
        self.assertIn(f"export NOTE={shlex.quote('two words')}", lines)

    def test_the_interval_is_the_pilot_s_and_overridable(self):
        self.assertIn(f'--interval ${{EPICRUN_PRMON_INTERVAL:-{epicrun.PRMON_INTERVAL_S}}}',
                      self.script)
        self.assertEqual(epicrun.PRMON_INTERVAL_S, 60)

    def test_the_script_is_valid_shell(self):
        for run in ('python run.sh',
                    'a | b > c 2>&1',
                    "sh -c 'echo nested'",
                    'for i in 1 2; do echo $i; done'):
            script = "\n".join(epicrun.run_script_lines(dict(SPEC, run=run))) + "\n"
            proc = subprocess.run(['sh', '-n'], input=script, text=True,
                                  capture_output=True)
            self.assertEqual(proc.returncode, 0, f'{run}: {proc.stderr}')


if __name__ == '__main__':
    unittest.main()
