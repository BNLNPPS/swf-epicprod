"""The payload's patched Rucio client (docs/EPICPROD_PAYLOAD.md item 22):
the shipped client and patch, and which client a token step uses, over a
fake image client. No catalog, no PanDA."""
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest

PAYLOAD = os.path.join(os.path.dirname(__file__), '..', 'swf_epicprod', 'payload')
sys.path.insert(0, PAYLOAD)
import tokens  # noqa: E402


def fake_image_client(root, version, patched):
    """A rucio package under root: its version and whether its upload
    client carries the token upload patch."""
    pkg = os.path.join(root, 'rucio')
    os.makedirs(os.path.join(pkg, 'client'))
    for name in ('__init__.py', os.path.join('client', '__init__.py')):
        open(os.path.join(pkg, name), 'w').close()
    with open(os.path.join(pkg, 'version.py'), 'w') as f:
        f.write(f'def version_string():\n    return {version!r}\n')
    with open(os.path.join(pkg, 'client', 'uploadclient.py'), 'w') as f:
        f.write('def get_upload_token():\n    pass\n' if patched else 'pass\n')


def patched_client_cli(image_root, dest):
    env = dict(os.environ, PYTHONPATH=image_root)
    return subprocess.run([sys.executable, os.path.join(PAYLOAD, 'tokens.py'),
                           'patched-client', '--dir', dest],
                          capture_output=True, text=True, env=env)


class ShippedClientTest(unittest.TestCase):
    def test_shipped_client_is_the_version_the_patch_is_for(self):
        with tarfile.open(tokens.RUCIO_CLIENT_SOURCE) as tar:
            names = tar.getnames()
            vcs = tar.extractfile('rucio-clients/lib/rucio/vcsversion.py').read().decode()
        self.assertIn('rucio-clients/LICENSE', names)
        self.assertIn(f"VERSION = '{tokens.RUCIO_CLIENT_VERSION}'", vcs)

    def test_unpacked_and_patched_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, 'rucio-clients')
            lib = tokens.patched_client(dest)
            upload = os.path.join(lib, 'rucio', 'client', 'uploadclient.py')
            with open(upload) as f:
                self.assertIn('get_upload_token', f.read())
            os.utime(upload, (0, 0))
            self.assertEqual(tokens.patched_client(dest), lib)
            self.assertEqual(os.stat(upload).st_mtime, 0)


class WhichClientTest(unittest.TestCase):
    def test_image_client_with_the_patch_is_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_image_client(tmp, '41.0.0', patched=True)
            done = patched_client_cli(tmp, os.path.join(tmp, 'out'))
        self.assertEqual(done.returncode, tokens.IMAGE_CLIENT_UPLOADS_WITH_TOKENS)
        self.assertEqual(done.stdout, '')

    def test_image_40_2_0_without_the_patch_gets_the_payloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_image_client(tmp, '40.2.0', patched=False)
            done = patched_client_cli(tmp, os.path.join(tmp, 'out'))
            self.assertEqual(done.returncode, 0, done.stderr)
            lib = done.stdout.strip()
            self.assertTrue(os.path.isfile(os.path.join(lib, 'rucio', 'client', 'uploadclient.py')))

    def test_other_unpatched_client_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_image_client(tmp, '38.5.0', patched=False)
            done = patched_client_cli(tmp, os.path.join(tmp, 'out'))
        self.assertEqual(done.returncode, 1)
        self.assertIn('38.5.0', done.stderr)


if __name__ == '__main__':
    unittest.main()
