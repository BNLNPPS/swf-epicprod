"""Exercise the site adapter without a Slurm allocation or credentials."""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / 'perlmutter/NERSC_Perlmutter_epic_es/epicprod-perlmutter-pilot-launch.sh'


def archive(path, files):
    with tarfile.open(path, 'w:gz') as output:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            output.addfile(info, io.BytesIO(data))
    return hashlib.sha256(path.read_bytes()).hexdigest()


class PerlmutterNativeLauncher(unittest.TestCase):
    def test_other_slurm_ranks_exit_without_starting_or_staging(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(['bash', str(LAUNCHER)], cwd=directory,
                                    env=dict(os.environ, SLURM_PROCID='1'),
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def run_adapter(self, directory, bad_checksum=False, slurm_end=None):
        root = Path(directory)
        fixtures = root / 'fixtures'; fixtures.mkdir()
        pilot = b'fixture pilot archive'
        pilot_sha = hashlib.sha256(pilot).hexdigest()
        native = b'''#!/bin/bash
python3 - "$@" <<'PY'
import json, os, sys
from pathlib import Path
Path('invocation.json').write_text(json.dumps({'args': sys.argv[1:], 'env': dict(os.environ)}))
PY
echo 'PRIVATE_CONSOLE_MARKER'
'''
        files = {'epic-native-pilot.sh': native, 'pilot3-native.tar.gz': pilot}
        manifest = {'pilot_archive_sha256': pilot_sha,
                    'files': {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}}
        files['manifest.json'] = json.dumps(manifest).encode()
        runtime_sha = archive(fixtures / 'runtime.tar.gz', files)
        channel_sha = archive(fixtures / 'channel.tar.gz', {
            'es-channel/python/yampl.cpython-313-x86_64-linux-gnu.so': b'fixture channel'})
        (fixtures / 'queuedata.json').write_text('{}')
        (fixtures / 'ddmendpoints.json').write_text('{}')
        harvester = root / 'harvester'; (harvester / 'globus').mkdir(parents=True)
        (harvester / 'globus/harvesterproxy').write_text('PRIVATE_PROXY_MARKER')
        bin_dir = root / 'bin'; bin_dir.mkdir()
        curl = bin_dir / 'curl'
        curl.write_text(f'''#!{sys.executable}
import shutil, sys
from pathlib import Path
args = sys.argv[1:]
if '-o' not in args:
    print('{{}}'); sys.exit(0)
i = args.index('-o'); url, dest = args[i-1], args[i+1]
name = ('runtime.tar.gz' if 'native-runtime-' in url else
        'channel.tar.gz' if 'es-channel-' in url else url.rsplit('/', 1)[-1])
shutil.copyfile(Path({str(fixtures)!r}) / name, dest)
''')
        curl.chmod(0o755)
        unshare = bin_dir / 'unshare'
        unshare.write_text(f'''#!{sys.executable}
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
Path('identity-invocation.json').write_text(json.dumps(args))
i = args.index('native-site-identity')
assert Path(args[i+1]).is_file() and Path(args[i+2]).is_file()
os.execvp(args[i+3], args[i+3:])
''')
        unshare.chmod(0o755)
        for name in ('squeue', 'sinfo', 'sleep'):
            stub = bin_dir / name; stub.write_text('#!/bin/bash\nexit 0\n'); stub.chmod(0o755)
        source = LAUNCHER.read_text()
        source = source.replace('HARVESTER_DIR=/global/common/software/m3763/panda-harvester',
                                f'HARVESTER_DIR={harvester}')
        source = source.replace('LAUNCH_PUBLISH_DIR=/global/cfs/cdirs/m3763/www/panda/jobs/$PQ/launch/${HARVESTER_WORKER_ID:-noworker}',
                                f'LAUNCH_PUBLISH_DIR={root}/published')
        replacements = {'RUNTIME_SHA256': '0' * 64 if bad_checksum else runtime_sha,
                        'PILOT_SHA256': pilot_sha, 'ES_CHANNEL_SHA256': channel_sha}
        for key, value in replacements.items():
            source = '\n'.join(f'{key}={value}' if line.startswith(key + '=') else line
                               for line in source.split('\n'))
        script = root / 'launch.sh'; script.write_text(source)
        access = root / 'access'
        env = dict(os.environ, PATH=str(bin_dir) + ':' + os.environ['PATH'], SLURM_PROCID='0',
                   SLURM_JOBID='123', SLURM_CPUS_ON_NODE='256', HARVESTER_WORKER_ID='456')
        env.pop('SLURM_JOB_END_TIME', None)
        if slurm_end is not None:
            env['SLURM_JOB_END_TIME'] = str(slurm_end)
        result = subprocess.run(['bash', str(script), 'NERSC_Perlmutter_epic_es', str(access)],
                                cwd=root, env=env, capture_output=True, text=True, timeout=15)
        return result, access / '123/0'

    def test_common_launcher_gets_site_identity_channel_and_production_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            result, work = self.run_adapter(directory)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            invocation = json.loads((work / 'invocation.json').read_text())
            env = invocation['env']
            self.assertEqual(env['EPICPROD_NATIVE_BATCH'], '1')
            self.assertEqual(env['ATHENA_PROC_NUMBER'], '256')
            self.assertEqual(env['HARVESTER_WORKER_ID'], '456')
            self.assertEqual(env['EPICPROD_NATIVE_ARCHIVE'], 'pilot3-native.tar.gz')
            self.assertEqual(env['EPICPROD_ES_CHANNEL'], str(work / 'es-channel'))
            self.assertIn('/global,/pscratch', env['APPTAINER_BINDPATH'])
            self.assertEqual(env['EPICPROD_MIN_TIME_FOR_NEW_JOB'], '600')
            identity = json.loads((work / 'identity-invocation.json').read_text())
            self.assertIn('--map-current-user', identity)
            self.assertIn('--mount', identity)
            self.assertIn('mount --bind "$1" /etc/passwd', identity[identity.index('-ceu')+1])
            self.assertTrue(any(line.split(':')[2] == str(os.getuid())
                                for line in (work / 'native-passwd').read_text().splitlines()))
            self.assertEqual((work / 'harvesterproxy').stat().st_mode & 0o777, 0o600)
            self.assertEqual((work / 'harvesterproxy').read_text(), 'PRIVATE_PROXY_MARKER')
            deadline = int((work / 'sitefeatures/shutdowntime').read_text())
            import time
            self.assertGreater(deadline - time.time(), 14350)
            self.assertLessEqual(deadline - time.time(), 14400)
            self.assertIn('--getjobrequests=1', invocation['args'])
            self.assertIn('--getjobfailures=1', invocation['args'])
            self.assertNotIn('PRIVATE_CONSOLE_MARKER', result.stdout)
            self.assertNotIn('PRIVATE_PROXY_MARKER', result.stdout)
            self.assertIn('PRIVATE_CONSOLE_MARKER', (work / 'native-pilot-console.log').read_text())
            self.assertEqual((work / 'native-pilot-console.log').stat().st_mode & 0o777, 0o600)

    def test_earlier_slurm_deadline_is_preserved(self):
        import time
        with tempfile.TemporaryDirectory() as directory:
            slurm_end = int(time.time()) + 3600
            result, work = self.run_adapter(directory, slurm_end=slurm_end)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(int((work / 'sitefeatures/shutdowntime').read_text()), slurm_end)

    def test_checksum_failure_never_runs_a_fallback_pilot(self):
        with tempfile.TemporaryDirectory() as directory:
            result, work = self.run_adapter(directory, bad_checksum=True)
            self.assertEqual(result.returncode, 65, result.stdout + result.stderr)
            self.assertFalse((work / 'invocation.json').exists())
            self.assertIn('no pilot started', result.stdout)
