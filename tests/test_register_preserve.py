"""Preserve first (docs/EPICPROD_PAYLOAD.md item 12): the registration
script's copy-home and register-in-place steps over fakes: a fake door
(xrdfs/xrdcp by subprocess) and a fake catalog client. No storage, no
catalog."""
import os
import sys
import tempfile
import types
import unittest
import zlib
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'swf_epicprod', 'payload'))
import register_to_rucio as reg  # noqa: E402


class FakeProc:
    def __init__(self, rc=0, out='', err=''):
        self.returncode, self.stdout, self.stderr = rc, out, err


class FakeDoor:
    """A door with a dict of path -> bytes; answers xrdfs stat, xrdfs query
    checksum and xrdcp the way the script reads them."""

    def __init__(self):
        self.files = {}
        self.copies = []

    def run(self, argv, **kw):
        if argv[0] == 'xrdfs':
            _, door, verb, *rest = argv
            path = rest[-1]
            if path not in self.files:
                return FakeProc(rc=54, err='[ERROR] No such file')
            data = self.files[path]
            if verb == 'stat':
                return FakeProc(out=f'Path: {path}\nId: 1\nSize: {len(data)}\nFlags: 16\n')
            if verb == 'query':
                return FakeProc(out=f'adler32 {zlib.adler32(data) & 0xffffffff:08x}\n')
        if argv[0] == 'xrdcp':
            src, dst = argv[1], argv[2]
            path = dst.split('1094/', 1)[1]
            with open(src, 'rb') as fh:
                self.files[path] = fh.read()
            self.copies.append(path)
            return FakeProc()
        raise AssertionError(argv)


class PreserveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.local = os.path.join(self.tmp, 'out.root')
        with open(self.local, 'wb') as fh:
            fh.write(b'physics' * 1000)
        self.door = FakeDoor()
        self.logger = mock.Mock()
        patcher = mock.patch('subprocess.run', side_effect=self.door.run)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ['PANDAID'] = '123'

    def test_adler32_matches_zlib(self):
        with open(self.local, 'rb') as fh:
            data = fh.read()
        self.assertEqual(reg.local_adler32(self.local), f'{zlib.adler32(data) & 0xffffffff:08x}')

    def test_copies_home_and_verifies(self):
        got = reg.preserve(self.local, '/RECO/x/a.eicrecon.edm4eic.root',
                           'root://door:1094', '/eic/EPIC', 60, self.logger)
        self.assertEqual(got[0], '/RECO/x/a.eicrecon.edm4eic.root')
        self.assertEqual(got[1], 7000)
        self.assertEqual(self.door.copies, ['/eic/EPIC/RECO/x/a.eicrecon.edm4eic.root'])

    def test_never_overwrites_an_earlier_attempt(self):
        self.door.files['/eic/EPIC/RECO/x/a.eicrecon.edm4eic.root'] = b'earlier attempt'
        got = reg.preserve(self.local, '/RECO/x/a.eicrecon.edm4eic.root',
                           'root://door:1094', '/eic/EPIC', 60, self.logger)
        self.assertEqual(got[0], '/RECO/x/a.p123.eicrecon.edm4eic.root')
        self.assertEqual(self.door.files['/eic/EPIC/RECO/x/a.eicrecon.edm4eic.root'], b'earlier attempt')
        self.assertEqual(self.door.copies, ['/eic/EPIC/RECO/x/a.p123.eicrecon.edm4eic.root'])

    def test_a_copy_that_does_not_verify_is_none(self):
        real = self.door.run

        def truncating(argv, **kw):
            proc = real(argv, **kw)
            if argv[0] == 'xrdcp':
                path = argv[2].split('1094/', 1)[1]
                self.door.files[path] = self.door.files[path][:-1]
            return proc
        with mock.patch('subprocess.run', side_effect=truncating):
            got = reg.preserve(self.local, '/RECO/x/a.eicrecon.edm4eic.root',
                               'root://door:1094', '/eic/EPIC', 60, self.logger)
        self.assertIsNone(got)


class FakeClient:
    def __init__(self, replicas=(), fail=None):
        self.replicas = list(replicas)
        self.fail = fail
        self.added, self.attached, self.meta = [], [], []

    def list_replicas(self, dids, all_states=False):
        if self.fail == 'list':
            raise RuntimeError('503')
        return iter(self.replicas)

    def add_files_to_datasets(self, attachments, ignore_duplicate=False):
        if self.fail == 'add':
            raise RuntimeError('503')
        for a in attachments:
            self.added.append((a['rse'], a['dids']))
            self.attached.append((a['name'], a['dids']))

    def set_metadata(self, scope, name, key, value):
        self.meta.append((name, key, value))


class RegisterInPlaceTest(unittest.TestCase):
    def test_registers_attaches_and_counts(self):
        c = FakeClient()
        out = reg.register_in_place(c, 'epic', '/RECO/x/a.root', 'BNL-XRD', 7000, 'abcd1234', 100, mock.Mock())
        self.assertEqual(out, 'registered')
        self.assertEqual(c.added[0][0], 'BNL-XRD')
        self.assertEqual(c.added[0][1][0]['adler32'], 'abcd1234')
        self.assertEqual(c.attached[0][0], '/RECO/x')
        self.assertEqual(c.meta, [('/RECO/x/a.root', 'events', 100)])

    def test_adopts_an_available_replica(self):
        c = FakeClient(replicas=[{'name': '/RECO/x/a.root', 'states': {'BNL-XRD': 'AVAILABLE'}}])
        self.assertEqual(reg.register_in_place(c, 'epic', '/RECO/x/a.root', 'BNL-XRD', 1, 'x', None, mock.Mock()), 'adopted')
        self.assertEqual(c.added, [])

    def test_a_silent_catalog_raises_and_registers_nothing(self):
        for fail in ('list', 'add'):
            c = FakeClient(fail=fail)
            with self.assertRaises(reg.CatalogUnreachable):
                reg.register_in_place(c, 'epic', '/RECO/x/a.root', 'BNL-XRD', 1, 'x', 100, mock.Mock())
            self.assertEqual(c.meta, [])


if __name__ == '__main__':
    unittest.main()
