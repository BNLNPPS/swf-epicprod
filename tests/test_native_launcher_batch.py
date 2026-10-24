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

    def run_launcher(self, batch=True):
        env = {k: v for k, v in os.environ.items() if not k.startswith(('APPTAINER_', 'SINGULARITY_'))}
        env.update(PATH=str(self.bin) + os.pathsep + env['PATH'], EPICPROD_NATIVE_IMAGE=str(self.image),
                   EPICPROD_NATIVE_ARCHIVE='pilot3-native.tar.gz', EPICPROD_NATIVE_ASSETS=str(self.top),
                   EPICPROD_NATIVE_PROXY='x509up_u1000', X509_USER_PROXY='/ce/spool/elsewhere',
                   SLURM_TMPDIR=str(self.slurm), SLURM_JOB_ID='123')
        if batch:
            env['EPICPROD_NATIVE_BATCH'] = '1'
        return subprocess.run(['bash', str(LAUNCHER), '-q', 'UM_GREX_PanDA_1'], cwd=self.here, env=env,
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


if __name__ == '__main__':
    unittest.main()
