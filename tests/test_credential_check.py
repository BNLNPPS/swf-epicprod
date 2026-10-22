"""The credential check with the JLab token beside the eicprod proxy
(swf_epicprod.credential_check): which entries count toward the exit."""
import json
import os
import unittest
from unittest import mock

from swf_epicprod import credential_check as cc


def run(token_status, proxy_days):
    """main()'s exit and entries, with the token and the eicprod proxy's
    days left given and every other credential ok."""
    def x509_exp(path):
        days = proxy_days if 'eicprod' in path else 300.0
        return cc.time.time() + days * 86400.0

    env = {'X509_USER_PROXY': '/robot', 'EVGEN_X509_PROXY': '/eicprod',
           'PANDA_CONFIG_ROOT': '/pathena'}
    token = {'credential': 'jlab_token', 'kind': 'token', 'path': '', 'status': token_status}
    with mock.patch.dict(os.environ, env), \
            mock.patch.object(cc.os.path, 'exists', return_value=True), \
            mock.patch.object(cc, '_x509_exp', side_effect=x509_exp), \
            mock.patch.object(cc, '_jwt_exp', return_value=cc.time.time() + 200 * 86400.0), \
            mock.patch.object(cc, '_jlab_token', return_value=token), \
            mock.patch('builtins.print') as printed:
        code = cc.main()
    entries = json.loads(printed.call_args[0][0])['credentials']
    return code, {e['credential']: e['status'] for e in entries}


class CredentialCheckTest(unittest.TestCase):
    def test_token_ok_supersedes_an_expiring_proxy(self):
        code, status = run('ok', 6.7)
        self.assertEqual(code, 0)
        self.assertEqual(status['evgen_output_proxy'], 'superseded')

    def test_token_ok_supersedes_an_expired_proxy(self):
        code, status = run('ok', -1.0)
        self.assertEqual(code, 0)
        self.assertEqual(status['evgen_output_proxy'], 'superseded')

    def test_token_unavailable_leaves_the_proxy_counting(self):
        self.assertEqual(run('unavailable', 6.7), (3, {
            'jlab_token': 'unavailable', 'panda_oidc_token': 'ok',
            'bnl_rucio_proxy': 'ok', 'evgen_output_proxy': 'expiring'}))

    def test_neither_working_is_an_error(self):
        code, status = run('unavailable', -1.0)
        self.assertEqual(code, 4)
        self.assertEqual(status['evgen_output_proxy'], 'expired')

    def test_token_unavailable_with_a_valid_proxy_is_healthy(self):
        self.assertEqual(run('unavailable', 30.0)[0], 0)


if __name__ == '__main__':
    unittest.main()
