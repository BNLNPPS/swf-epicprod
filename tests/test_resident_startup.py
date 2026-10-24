"""A launched resident can be alive before a command-line scan finds it."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

RUNNER = Path(__file__).resolve().parents[1] / 'swf_epicprod/payload/run.sh'


class ResidentStartup(unittest.TestCase):
    def start(self, switch, crashes=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = RUNNER.read_text()
            block = source.split('      resident_wait=()\n', 1)[1].split(
                '      echo "resident eicrecon up after ${i} s"', 1)[0]
            # A deterministic missed process scan reproduces the real Google
            # failure without depending on the machine's fork/exec scheduling.
            (root / 'pgrep').write_text('#!/bin/sh\nexit 1\n')
            (root / 'pgrep').chmod(0o755)
            resident = root / 'eicrecon'
            if crashes:
                resident.write_text('#!/bin/sh\necho genuine-startup-failure\nexit 7\n')
            else:
                resident.write_text('#!/usr/bin/env python3\n'
                                    'import os,socket,time\n'
                                    'time.sleep(0.2)\n'
                                    's=socket.socket(socket.AF_UNIX)\n'
                                    's.bind(os.environ["EPICPROD_RECO_SOCKET"])\n'
                                    'time.sleep(10)\n')
            resident.chmod(0o755)
            env = dict(os.environ, PATH=str(root) + os.pathsep + os.environ['PATH'],
                       EPICPROD_RECO_SOCKET=str(root / 'resident.sock'),
                       EPICPROD_RECO_STARTUP_S='3', DETECTOR_COMPACT='geometry.xml')
            env.pop('EPICPROD_SWITCH_RESIDENT_PID_WAIT', None)
            if switch is not None:
                env['EPICPROD_SWITCH_RESIDENT_PID_WAIT'] = str(switch)
            script = ('set -e\ntrap \'kill "${resident_pid:-}" 2>/dev/null || true\' EXIT\n'
                      'resident_wait=()\n' + block + '\necho SOCKET_READY\n')
            return subprocess.run(['bash', '-c', script], env=env, text=True,
                                  capture_output=True, timeout=6)

    def test_live_child_survives_a_missed_process_scan(self):
        legacy = self.start(0)
        self.assertEqual(legacy.returncode, 86, legacy.stdout + legacy.stderr)
        fixed = self.start(1)
        self.assertEqual(fixed.returncode, 0, fixed.stdout + fixed.stderr)
        self.assertIn('SOCKET_READY', fixed.stdout)

    def test_real_child_exit_still_fails_with_its_log(self):
        result = self.start(1, crashes=True)
        self.assertEqual(result.returncode, 86, result.stdout + result.stderr)
        self.assertIn('genuine-startup-failure', result.stdout)

    def test_unset_switch_uses_pid_wait_and_still_detects_real_exit(self):
        result = self.start(None)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('SOCKET_READY', result.stdout)
        failed = self.start(None, crashes=True)
        self.assertEqual(failed.returncode, 86, failed.stdout + failed.stderr)
