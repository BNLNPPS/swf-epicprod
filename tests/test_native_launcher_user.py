"""The native launcher's user name for the Rucio client (pilot/native/job-user.sh)."""
import getpass
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / 'pilot/native/job-user.sh'


def fake_id(directory, name_output, name_exit, uid='1000'):
    """A mocked `id`: `-un` prints name_output and exits name_exit; `-u` prints the uid."""
    path = Path(directory) / 'id'
    path.write_text('#!/bin/bash\ncase "$1" in\n  -un) printf "%%s" "%s"; exit %d ;;\n  -u) echo %s ;;\nesac\n'
                    % (name_output, name_exit, uid))
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def run(directory, env):
    probe = 'set -euo pipefail; source %s; printf "%%s|%%s" "$USER" "$LOGNAME"' % SCRIPT
    full = {'PATH': directory + ':' + os.environ['PATH']}
    full.update(env)
    out = subprocess.run(['bash', '-c', probe], env=full, capture_output=True, text=True, timeout=10)
    assert out.returncode == 0, out.stderr
    return out.stdout.split('|')


class JobUserTests(unittest.TestCase):
    def test_id_printing_the_number_and_failing_gives_uid_name_only(self):
        with tempfile.TemporaryDirectory() as d:
            fake_id(d, '1000\n', 1)
            self.assertEqual(run(d, {}), ['uid1000', 'uid1000'])

    def test_id_failing_silently_gives_uid_name(self):
        with tempfile.TemporaryDirectory() as d:
            fake_id(d, '', 1)
            self.assertEqual(run(d, {}), ['uid1000', 'uid1000'])

    def test_passwd_name_is_used_when_id_succeeds(self):
        with tempfile.TemporaryDirectory() as d:
            fake_id(d, 'osg\n', 0)
            self.assertEqual(run(d, {}), ['osg', 'osg'])

    def test_existing_user_and_logname_are_kept(self):
        with tempfile.TemporaryDirectory() as d:
            fake_id(d, 'osg\n', 0)
            self.assertEqual(run(d, {'USER': 'pilot', 'LOGNAME': 'other'}), ['pilot', 'other'])

    def test_getuser_without_passwd_lookup_needs_the_variable(self):
        """The premise: with no passwd entry, getpass.getuser() lives on USER alone."""
        with mock.patch('pwd.getpwuid', side_effect=KeyError(1000)):
            with mock.patch.dict(os.environ, {}, clear=True):
                with self.assertRaises((OSError, KeyError)):  # OSError from 3.13, KeyError before
                    getpass.getuser()
            with mock.patch.dict(os.environ, {'USER': 'uid1000'}, clear=True):
                self.assertEqual(getpass.getuser(), 'uid1000')


if __name__ == '__main__':
    unittest.main()
