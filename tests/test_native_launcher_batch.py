"""The native launcher on a batch allocation with no glidein (GREX): with
EPICPROD_NATIVE_BATCH=1 it links the allocation's shipped files into its
pilot directory, takes the shipped proxy over a CE's rewritten path, and
starts the image once through Apptainer with the scratch bound; without
the switch and outside a container it refuses as before."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

LAUNCHER = Path(__file__).resolve().parents[1] / 'pilot/native/epic-native-pilot.sh'


class BatchLaunch(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.top = self.root / 'allocation'; self.top.mkdir()
        self.here = self.top / 'pilot-1'; self.here.mkdir()
        self.slurm = self.root / 'slurm-tmp'; self.slurm.mkdir()
        self.image = self.root / 'image'; self.image.mkdir()
        for f in ('pilot3-native.tar.gz', 'job-user.sh', 'glidein-deadline.sh', 'certificates.tar.gz',
                  'rucio.cfg', 'runtime-policy.py', 'queue-runtime-policy.json', 'x509up_u1000'):
            (self.top / f).write_text('x')
        self.bin = self.root / 'bin'; self.bin.mkdir()
        self.record = self.root / 'apptainer-args'
        fake = self.bin / 'apptainer'
        fake.write_text('#!/bin/bash\nprintf "%s\\n" "$@" > ' + str(self.record) + '\n'
                        'echo "X509_USER_PROXY=$X509_USER_PROXY" >> ' + str(self.record) + '\n')
        fake.chmod(0o755)

    def run_launcher(self, batch=True, launcher=LAUNCHER):
        env = {k: v for k, v in os.environ.items() if not k.startswith(('APPTAINER_', 'SINGULARITY_'))}
        env.update(PATH=str(self.bin) + os.pathsep + env['PATH'], EPICPROD_NATIVE_IMAGE=str(self.image),
                   EPICPROD_NATIVE_ARCHIVE='pilot3-native.tar.gz', EPICPROD_NATIVE_ASSETS=str(self.top),
                   EPICPROD_NATIVE_PROXY='x509up_u1000', X509_USER_PROXY='/ce/spool/elsewhere',
                   SLURM_TMPDIR=str(self.slurm), SLURM_JOB_ID='123')
        if batch:
            env['EPICPROD_NATIVE_BATCH'] = '1'
        return subprocess.run(['bash', str(launcher), '-q', 'UM_GREX_PanDA_1'], cwd=self.here, env=env,
                              capture_output=True, text=True)

    def test_batch_starts_the_image_with_its_files_proxy_and_scratch(self):
        result = self.run_launcher()
        self.assertEqual(result.returncode, 0, result.stderr)
        args = self.record.read_text().splitlines()
        self.assertEqual(args[:2], ['exec', '--no-home'])
        joined = ' '.join(args)
        for bind in ('/cvmfs:/cvmfs', '%s:%s' % (self.here, self.here), '%s:%s' % (self.top, self.top),
                     '%s:%s' % (self.slurm, self.slurm)):
            self.assertIn('--bind ' + bind, joined)
        self.assertIn(str(self.image) + ' /bin/bash ' + str(LAUNCHER) + ' -q UM_GREX_PanDA_1', joined)
        self.assertIn('X509_USER_PROXY=%s/x509up_u1000' % self.here, args[-1])
        for f in ('pilot3-native.tar.gz', 'job-user.sh', 'runtime-policy.py', 'x509up_u1000'):
            self.assertTrue((self.here / f).is_symlink(), f)

    def test_outside_a_container_without_the_switch_it_refuses_as_before(self):
        result = self.run_launcher(batch=False)
        self.assertEqual(result.returncode, 69)
        self.assertIn('not inside a container', result.stderr)
        self.assertFalse(self.record.exists())

    def test_missing_session_is_created_before_runtime_exec(self):
        session = self.root / 'private session'
        launcher = self.root / 'launcher.sh'
        launcher.write_text(LAUNCHER.read_text().replace(
            'SESSION_DIR=/var/lib/apptainer/mnt/session', 'SESSION_DIR=' + repr(str(session))))
        unshare = self.bin / 'unshare'
        unshare.write_text('#!/bin/bash\nif [[ $1 == --help ]]; then echo --map-current-user; exit 0; fi\n'
                           'while [[ $1 != /bin/bash ]]; do shift; done\nexec "$@"\n')
        unshare.chmod(0o755)
        mount = self.bin / 'mount'
        mount.write_text('#!/bin/bash\nexit 0\n')
        mount.chmod(0o755)
        result = self.run_launcher(launcher=launcher)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(session.is_dir())
        args = self.record.read_text().splitlines()
        self.assertEqual(args[:2], ['exec', '--no-home'])
        self.assertIn(str(self.image), args)
        self.assertIn(str(launcher), args)

    def test_old_batch_unshare_keeps_the_established_runtime_path(self):
        launcher = self.root / 'launcher.sh'
        launcher.write_text(LAUNCHER.read_text().replace(
            'SESSION_DIR=/var/lib/apptainer/mnt/session',
            'SESSION_DIR=' + repr(str(self.root / 'absent-session'))))
        unshare = self.bin / 'unshare'
        unshare.write_text('#!/bin/bash\nif [[ $1 == --help ]]; then echo old-unshare; exit 0; fi\nexit 99\n')
        unshare.chmod(0o755)
        result = self.run_launcher(launcher=launcher)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('older host unshare', result.stdout)
        self.assertIn(str(self.image), self.record.read_text())

    def test_ce_sandbox_has_stable_paths_outside_the_private_varlib(self):
        launcher = self.root / 'ce-launcher.sh'
        # Exercise the CE-path branch with an unprivileged sandbox fixture.
        launcher.write_text(LAUNCHER.read_text().replace(
            '[[ $HERE == /var/lib/* ]]', '[[ $HERE == ' + str(self.here) + ' ]]'))
        result = self.run_launcher(launcher=launcher)
        self.assertEqual(result.returncode, 0, result.stderr)
        args = self.record.read_text()
        for value in (str(self.here) + ':/srv', str(self.top) + ':/epicprod-assets',
                      'EPICPROD_NATIVE_ASSETS=/epicprod-assets', '_CONDOR_SCRATCH_DIR=/srv'):
            self.assertIn(value, args)
        self.assertIn('--pwd\n/srv\n', args)
        self.assertIn('/bin/bash\n/epicprod-launcher.sh\n', args)


if __name__ == '__main__':
    unittest.main()


class InsideTheImage(unittest.TestCase):
    """Inside the image (on GREX after the batch step, on OSG as started by the
    glidein), the pilot receives this pilot directory's queuedata.json, the
    per-pilot copy epicprod-multipilot.sh writes with its time floor and
    maxtime, unless the arguments already name queue data."""

    def setUp(self):
        import tarfile
        self.root = Path(tempfile.mkdtemp())
        self.top = self.root / 'allocation'; self.top.mkdir()
        self.here = self.top / 'pilot-1'; self.here.mkdir()
        self.image = self.root / 'image'; self.image.mkdir()
        src = Path(__file__).resolve().parents[1] / 'pilot/native'
        for f in ('job-user.sh', 'glidein-deadline.sh'):
            (self.top / f).write_text((src / f).read_text())
        tree = self.root / 'tree' / 'pilot3'; tree.mkdir(parents=True)
        (tree / 'pilot.py').write_text('')
        with tarfile.open(self.top / 'pilot3-native.tar.gz', 'w:gz') as archive:
            archive.add(tree, arcname='pilot3')
        (self.top / 'x509up_u1000').write_text('proxy')
        (self.here / 'queuedata.json').write_text('{"timefloor": 55}')
        self.bin = self.root / 'bin'; self.bin.mkdir()
        self.calls = self.root / 'python3-calls'
        fake = self.bin / 'python3'
        fake.write_text('#!/bin/bash\nprintf "%s " "$@" >> ' + str(self.calls) + '\necho >> '
                        + str(self.calls) + '\ncat >/dev/null\nexit 0\n')
        fake.chmod(0o755)

    def run_launcher(self, *args):
        env = {k: v for k, v in os.environ.items() if not k.startswith(('APPTAINER_', 'SINGULARITY_'))}
        env.update(PATH=str(self.bin) + os.pathsep + env['PATH'], APPTAINER_CONTAINER=str(self.image),
                   EPICPROD_NATIVE_IMAGE=str(self.image), EPICPROD_NATIVE_ARCHIVE='pilot3-native.tar.gz',
                   EPICPROD_NATIVE_ASSETS=str(self.top), EPICPROD_NATIVE_PROXY='x509up_u1000',
                   EPICPROD_NATIVE_BATCH='1', X509_USER_PROXY='/ce/spool/elsewhere')
        return subprocess.run(['bash', str(LAUNCHER), '-q', 'UM_GREX_PanDA_1', *args], cwd=self.here,
                              env=env, capture_output=True, text=True)

    def test_pilot_gets_this_pilots_queuedata(self):
        result = self.run_launcher()
        self.assertEqual(result.returncode, 0, result.stderr)
        last = self.calls.read_text().strip().splitlines()[-1]
        self.assertIn('pilot3/pilot.py -q UM_GREX_PanDA_1 --queuedata-url file://%s/queuedata.json'
                      % self.here, last)

    def test_named_queuedata_is_kept(self):
        result = self.run_launcher('--queuedata-url', 'http://elsewhere/q.json')
        self.assertEqual(result.returncode, 0, result.stderr)
        last = self.calls.read_text().strip().splitlines()[-1]
        self.assertIn('--queuedata-url http://elsewhere/q.json', last)
        self.assertEqual(last.count('--queuedata-url'), 1)
