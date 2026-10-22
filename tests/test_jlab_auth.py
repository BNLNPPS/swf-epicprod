"""The ops agent's JLab credentials (swf_epicprod.jlab_auth): which door
gets a token, what each environment carries, and the proxy fallback."""
import os
import unittest
from unittest import mock

from swf_epicprod import jlab_auth


class DoorAccessTest(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {'X509_USER_PROXY': '/robot',
                                                'BEARER_TOKEN_FILE': '/stray'})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_token_door_gets_a_path_scoped_token_over_tls(self):
        with mock.patch.object(jlab_auth, 'panda_token', return_value='tok') as token:
            url, env, how = jlab_auth.door_access('root://epicxrd1.sdcc.bnl.gov:1094',
                                                  '//eic/EPIC//TEST/f.root')
        self.assertEqual((url, how), ('roots://epicxrd1.sdcc.bnl.gov:1094', 'token'))
        self.assertEqual(env['XrdSecPROTOCOL'], 'ztn')
        self.assertEqual(env['BEARER_TOKEN'], 'tok')
        self.assertNotIn('X509_USER_PROXY', env)
        self.assertNotIn('BEARER_TOKEN_FILE', env)
        token.assert_called_once_with('storage', audience='epicxrd1.sdcc.bnl.gov',
                                      scope='storage.read:/eic/EPIC/TEST/f.root')

    def test_read_port_is_not_a_token_door(self):
        with mock.patch.object(jlab_auth, 'panda_token') as token:
            url, env, how = jlab_auth.door_access('root://epicxrd1.sdcc.bnl.gov:1095', '/eic/EPIC/f')
        token.assert_not_called()
        self.assertEqual((url, how), ('root://epicxrd1.sdcc.bnl.gov:1095', 'none'))
        self.assertNotIn('X509_USER_PROXY', env)

    def test_jlab_scope_is_the_path_less_the_rse_oidc_base_path(self):
        for path, scope in (('//volatile/eic/EPIC//RECO/f.root', '/eic/EPIC/RECO/f.root'),
                            ('/work/eic3/EPIC/eic/LOG/t/log.tgz', '/eic/LOG/t/log.tgz'),
                            ('/other/f', '/other/f')):
            with mock.patch.object(jlab_auth, 'panda_token', return_value='tok') as token:
                url, _env, how = jlab_auth.door_access('root://dtn-rucio.jlab.org:1094', path)
            self.assertEqual((url, how), ('roots://dtn-rucio.jlab.org:1094', 'token'))
            token.assert_called_once_with('storage', audience='dtn-rucio.jlab.org',
                                          scope=f'storage.read:{scope}')

    def test_other_door_gets_the_proxy(self):
        url, env, how = jlab_auth.door_access('root://xgate.hec.lancs.ac.uk:1094', '/cephfs/x',
                                              proxy='/eicprod')
        self.assertEqual((url, how), ('root://xgate.hec.lancs.ac.uk:1094', 'x509'))
        self.assertEqual(env['X509_USER_PROXY'], '/eicprod')
        self.assertNotIn('BEARER_TOKEN', env)

    def test_token_door_falls_back_to_the_proxy_when_no_token_comes(self):
        with mock.patch.object(jlab_auth, 'panda_token', side_effect=jlab_auth.NoToken('no')):
            url, env, how = jlab_auth.door_access('root://epicxrd1.sdcc.bnl.gov:1094', '/eic/EPIC/f',
                                                  proxy='/eicprod')
        self.assertEqual((url, how), ('root://epicxrd1.sdcc.bnl.gov:1094', 'x509'))
        self.assertEqual(env['X509_USER_PROXY'], '/eicprod')


class RucioClientTest(unittest.TestCase):
    def test_no_token_and_no_proxy_names_both(self):
        with mock.patch.object(jlab_auth, '_token_client', side_effect=jlab_auth.NoToken('refused')):
            with self.assertRaises(RuntimeError) as caught:
                jlab_auth.rucio_client(proxy=None)
        self.assertIn('refused', str(caught.exception))
        self.assertIn('no proxy', str(caught.exception))

    def test_token_mapping_to_another_account_is_refused(self):
        client = mock.Mock(jlab_auth='token')
        client.whoami.return_value = {'account': 'eicread'}
        with mock.patch.object(jlab_auth, '_token_client', return_value=client):
            with self.assertRaises(RuntimeError):
                jlab_auth.rucio_client(proxy=None)


class ExpiryTest(unittest.TestCase):
    def test_unreadable_token_has_no_expiry(self):
        self.assertEqual(jlab_auth._expiry('not-a-jwt'), 0.0)


if __name__ == '__main__':
    unittest.main()
