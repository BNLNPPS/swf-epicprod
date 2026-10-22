"""How the payload signs in to the PanDA server for its tokens
(docs/EPICPROD_PAYLOAD.md item 22): the pilot's PanDA token, else the
pilot's proxy, else not at all; and the reply read as JSON. No PanDA."""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'swf_epicprod', 'payload'))
import tokens  # noqa: E402

CLEAN = {k: '' for k in ('OIDC_REFRESHED_AUTH_TOKEN', 'OIDC_AUTH_TOKEN', 'PANDA_AUTH_TOKEN',
                         'OIDC_AUTH_ORIGIN', 'PANDA_AUTH_ORIGIN', 'PILOT_X509_USER_PROXY')}


class FakeReply:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self.body


class PandaLoginTest(unittest.TestCase):
    def test_pilot_token_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            token = os.path.join(tmp, 'panda_token')
            with open(token, 'w') as f:
                f.write('tok\n')
            proxy = os.path.join(tmp, 'x509up')
            open(proxy, 'w').close()
            env = dict(CLEAN, OIDC_AUTH_TOKEN=token, OIDC_AUTH_ORIGIN='EIC.production',
                       PILOT_X509_USER_PROXY=proxy)
            with mock.patch.dict(os.environ, env):
                login = tokens.panda_login()
        self.assertEqual(login, {'headers': {'Authorization': 'Bearer tok',
                                             'Origin': 'EIC.production'}})

    def test_pilot_proxy_without_a_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            proxy = os.path.join(tmp, 'x509up')
            open(proxy, 'w').close()
            with mock.patch.dict(os.environ, dict(CLEAN, PILOT_X509_USER_PROXY=proxy)):
                self.assertEqual(tokens.panda_login(), {'cert': proxy})

    def test_neither(self):
        with mock.patch.dict(os.environ, dict(CLEAN, PILOT_X509_USER_PROXY='/no/such/proxy')):
            with self.assertRaises(tokens.NoPandaToken):
                tokens.panda_login()

    def test_asks_for_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            proxy = os.path.join(tmp, 'x509up')
            open(proxy, 'w').close()
            reply = FakeReply({'success': True, 'data': {'user_proxy': 'access\n'}})
            with mock.patch.dict(os.environ, dict(CLEAN, PILOT_X509_USER_PROXY=proxy)), \
                    mock.patch.object(tokens.requests, 'get', return_value=reply) as get:
                self.assertEqual(tokens.panda_token('rucio'), 'access')
        kwargs = get.call_args.kwargs
        self.assertEqual(kwargs['cert'], proxy)
        self.assertEqual(kwargs['headers'], {'Accept': 'application/json'})
        self.assertEqual(kwargs['params'], {'client_name': 'rucio'})


if __name__ == '__main__':
    unittest.main()
