"""The upload step without the X.509 client credential
(EPICPROD_SWITCH_UPLOAD_NO_X509, docs/EPICPROD_PAYLOAD.md item 27):
dropped only with the switch on and a token Rucio config. No catalog."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'swf_epicprod', 'payload'))
import tokens  # noqa: E402

X509 = {'X509_USER_PROXY': '/creds/eicprod-proxy-for-jlab',
        'X509_USER_CERT': '/creds/cert', 'X509_USER_KEY': '/creds/key'}


def cfg(tmp, auth_type):
    path = os.path.join(tmp, 'rucio.cfg')
    with open(path, 'w') as f:
        f.write(f'[client]\nauth_type = {auth_type}\n')
        if auth_type == 'oidc':
            f.write(f'auth_token_file_path = {tmp}/rucio_token\n')
    return path


class UploadWithoutX509Test(unittest.TestCase):
    def test_switch_on_token_config_drops_the_proxy(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(X509, X509_CERT_DIR='/ca', EPICPROD_SWITCH_UPLOAD_NO_X509='1',
                       RUCIO_CONFIG=cfg(tmp, 'oidc'))
            dropped = tokens.upload_without_x509(env)
        self.assertEqual(dropped, list(tokens.X509_CLIENT_VARS))
        for k in tokens.X509_CLIENT_VARS:
            self.assertNotIn(k, env)
        self.assertEqual(env['X509_CERT_DIR'], '/ca')  # the CA directory stays

    def test_switch_off_keeps_the_proxy(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(X509, RUCIO_CONFIG=cfg(tmp, 'oidc'))
            self.assertEqual(tokens.upload_without_x509(env), [])
        self.assertEqual(env['X509_USER_PROXY'], X509['X509_USER_PROXY'])

    def test_x509_rucio_config_keeps_the_proxy(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(X509, EPICPROD_SWITCH_UPLOAD_NO_X509='1', RUCIO_CONFIG=cfg(tmp, 'x509_proxy'))
            self.assertEqual(tokens.upload_without_x509(env), [])
        self.assertEqual(env['X509_USER_PROXY'], X509['X509_USER_PROXY'])

    def test_no_rucio_config_keeps_the_proxy(self):
        env = dict(X509, EPICPROD_SWITCH_UPLOAD_NO_X509='1')
        self.assertEqual(tokens.upload_without_x509(env), [])
        self.assertIn('X509_USER_PROXY', env)

    def test_only_variables_present_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {'X509_USER_PROXY': '/p', 'EPICPROD_SWITCH_UPLOAD_NO_X509': '1',
                   'RUCIO_CONFIG': cfg(tmp, 'oidc')}
            self.assertEqual(tokens.upload_without_x509(env), ['X509_USER_PROXY'])


if __name__ == '__main__':
    unittest.main()
