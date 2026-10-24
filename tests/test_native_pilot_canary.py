"""Execution and image checks for the native ePIC pilot: in place when the image matches, nested otherwise."""
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1] / 'pilot/native-canary'
SPEC = importlib.util.spec_from_file_location('native_container', ROOT / 'native_container.py')
native = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(native)


class NativePilotCanaryTests(unittest.TestCase):
    def test_matching_image_reuses_environment_and_sees_replaced_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            image = work / 'campaign-image'
            image.mkdir()
            proxy = work / 'pilot-proxy'
            proxy.write_text('before')
            job = SimpleNamespace(imagename=str(image), workdir=str(work), jobid=12)
            env = dict(APPTAINER_CONTAINER=str(image), X509_USER_PROXY=str(proxy))
            with patch.dict(os.environ, env):
                command = native.wrapper('export PILOT_X509_USER_PROXY="$X509_USER_PROXY"; '
                                         'test "$(cat "$PILOT_X509_USER_PROXY")" = after', job=job)
                refreshed = work / 'refreshed'
                refreshed.write_text('after')
                refreshed.replace(proxy)
                result = subprocess.run(['bash', '-c', command], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn('apptainer exec', command)
            self.assertNotIn('atlasLocalSetup', command)

    def test_another_campaign_runs_nested_in_its_own_image(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            old, wanted = work / 'old/image', work / 'wanted/image'
            old.mkdir(parents=True)
            wanted.mkdir(parents=True)
            job = SimpleNamespace(imagename=str(wanted), workdir=str(work), jobid=12)
            with patch.dict(os.environ, {'APPTAINER_CONTAINER': str(old), 'PILOT_HOME': str(work)}):
                with patch.object(native, 'runtime', return_value='/fake/apptainer'):
                    with patch.object(native.os.path, 'isdir', side_effect=lambda d: d != native.SESSION_DIR):
                        command = native.wrapper('touch should-not-run', job=job)
                    with patch.object(native.os.path, 'isdir', return_value=True):
                        direct = native.wrapper('touch should-not-run', job=job)
            self.assertFalse((work / 'should-not-run').exists())
            # the outer container lacks the session directory: a private namespace carries it
            self.assertTrue(command.startswith('unshare --user --map-current-user --keep-caps --mount --propagation private /bin/bash -c '))
            self.assertIn('mount -t tmpfs -o size=1m,nodev,nosuid tmpfs /var/lib && mkdir -p /var/lib/apptainer/mnt/session && exec /fake/apptainer exec --no-home --bind /cvmfs:/cvmfs', command)
            self.assertIn(str(wanted) + ' /bin/bash -c ', command)
            self.assertIn('--bind ' + str(work) + ':' + str(work), command)
            # with the directory present the runtime runs directly
            self.assertTrue(direct.startswith('/fake/apptainer exec --no-home --bind /cvmfs:/cvmfs'))
            self.assertFalse(native.same_image('image', str(wanted)))

    def test_direct_mode_refuses_another_campaign_before_the_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            old, wanted = work / 'old/image', work / 'wanted/image'
            old.mkdir(parents=True)
            wanted.mkdir(parents=True)
            job = SimpleNamespace(imagename=str(wanted), workdir=str(work), jobid=12)
            with patch.dict(os.environ, {'APPTAINER_CONTAINER': str(old), 'EPICPROD_NATIVE_NESTING': 'off'}):
                with self.assertRaisesRegex(RuntimeError, 'direct mode'):
                    native.wrapper('touch should-not-run', job=job)
            self.assertFalse((work / 'should-not-run').exists())

    def test_another_campaign_without_a_runtime_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            old, wanted = work / 'old/image', work / 'wanted/image'
            old.mkdir(parents=True)
            wanted.mkdir(parents=True)
            job = SimpleNamespace(imagename=str(wanted), workdir=str(work), jobid=12)
            with patch.dict(os.environ, {'APPTAINER_CONTAINER': str(old)}):
                with patch.object(native, 'runtime', return_value=None):
                    with self.assertRaisesRegex(RuntimeError, 'unavailable'):
                        native.wrapper('touch should-not-run', job=job)
            self.assertFalse((work / 'should-not-run').exists())

    def test_launcher_refuses_an_unexpected_outer_image(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            for name in ('old', 'wanted'):
                (work / name).mkdir()
            (work / 'pilot-proxy').write_text('synthetic-test-credential')
            env = dict(os.environ, APPTAINER_CONTAINER=str(work / 'old'),
                       EPICPROD_CANARY_IMAGE=str(work / 'wanted'),
                       EPICPROD_CANARY_TASK_ID='1', EPICPROD_CANARY_QUEUE='test',
                       EPICPROD_CANARY_ARCHIVE='unused')
            result = subprocess.run(['bash', str(ROOT / 'epic-native-canary.sh')],
                                    cwd=work, env=env, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 65)
            self.assertIn('refusing nested launch', result.stderr)


if __name__ == '__main__':
    unittest.main()


class JobScratch(unittest.TestCase):
    """Each job's own scratch: the payload sees it as _CONDOR_SCRATCH_DIR, it is
    removed after the payload exits with the exit code kept, the peaks are
    sampled into the job directory, a killed job's scratch is swept by the
    pilot's next job from its own ledger only, and EPICPROD_KEEP_TMPDIR=1 keeps it."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.batch = self.root / 'batch'; self.batch.mkdir()
        self.pilotdir = self.root / 'pilot'; self.pilotdir.mkdir()
        self.image = self.root / 'image'; self.image.mkdir()

    def job(self, jobid):
        work = self.pilotdir / ('PanDA_Pilot-%d' % jobid); work.mkdir()
        return SimpleNamespace(imagename=str(self.image), workdir=str(work), jobid=jobid)

    def run_job(self, jobid, executable, extra_env=None):
        job = self.job(jobid)
        env = dict(APPTAINER_CONTAINER=str(self.image), _CONDOR_SCRATCH_DIR=str(self.batch))
        env.update(extra_env or {})
        with patch.dict(os.environ, env):
            command = native.wrapper(executable, job=job)
            result = subprocess.run(['bash', '-c', command], capture_output=True, text=True,
                                    env=dict(os.environ, **env))
        return job, result

    def test_payload_scratch_is_its_own_removed_at_exit_and_peaks_recorded(self):
        job, result = self.run_job(7, 'echo "$_CONDOR_SCRATCH_DIR" > seen; '
                                      'mkdir -p "$_CONDOR_SCRATCH_DIR/FULL"; '
                                      'head -c 300000 /dev/zero > "$_CONDOR_SCRATCH_DIR/FULL/a.root"; exit 3')
        self.assertEqual(result.returncode, 3, result.stderr)
        scratch = self.batch / 'epicprod-scratch-7'
        self.assertEqual(Path(job.workdir, 'seen').read_text().strip(), str(scratch))
        self.assertFalse(scratch.exists())
        kb, files = native.scratch_peak(job.workdir)
        self.assertGreaterEqual(kb, 290)
        self.assertGreaterEqual(files, 3)
        self.assertTrue(Path(job.workdir).is_dir())

    def test_killed_jobs_scratch_is_swept_by_the_next_job_of_this_pilot_only(self):
        stale = self.batch / 'epicprod-scratch-5'; stale.mkdir()
        neighbour = self.batch / 'epicprod-scratch-6'; neighbour.mkdir()
        (self.pilotdir / native.SCRATCH_LEDGER).write_text(str(stale) + '\n')
        self.run_job(8, 'true')
        self.assertFalse(stale.exists())
        self.assertTrue(neighbour.exists())  # another pilot's job, not in this ledger

    def test_keep_tmpdir_keeps_the_scratch_and_the_stale_one(self):
        stale = self.batch / 'epicprod-scratch-5'; stale.mkdir()
        (self.pilotdir / native.SCRATCH_LEDGER).write_text(str(stale) + '\n')
        job, result = self.run_job(9, 'touch "$_CONDOR_SCRATCH_DIR/x"', {'EPICPROD_KEEP_TMPDIR': '1'})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.batch / 'epicprod-scratch-9' / 'x').exists())
        self.assertTrue(stale.exists())

    def test_slurm_scratch_is_the_base_when_there_is_no_condor_scratch(self):
        with patch.dict(os.environ, {'_CONDOR_SCRATCH_DIR': '', 'SLURM_TMPDIR': str(self.batch)}):
            self.assertEqual(native.scratch_base(str(self.pilotdir / 'w')), str(self.batch))
        with patch.dict(os.environ, {'_CONDOR_SCRATCH_DIR': '', 'SLURM_TMPDIR': '', 'SLURM_JOB_ID': ''}):
            self.assertEqual(native.scratch_base(str(self.pilotdir / 'w')), str(self.pilotdir))
