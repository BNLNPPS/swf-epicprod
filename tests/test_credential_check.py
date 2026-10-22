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


class GateDaysTest(unittest.TestCase):
    """The days map the agent records and the front's gate reads."""

    def test_superseded_expired_proxy_with_another_in_its_window_is_green(self):
        # swf-3's case: the token healthy, the eicprod proxy expired and
        # superseded, the PanDA token two days from expiry; the check says
        # warning, and the gate must read only what counts.
        from swf_epicprod.front import credential_verdict
        results = [
            {'credential': 'jlab_token', 'kind': 'token', 'status': 'ok'},
            {'credential': 'panda_oidc_token', 'status': 'expiring', 'days_left': 2.0},
            {'credential': 'bnl_rucio_proxy', 'status': 'ok', 'days_left': 30.0},
            {'credential': 'evgen_output_proxy', 'status': 'superseded', 'days_left': -0.5},
        ]
        days = cc.gate_days(results)
        self.assertEqual(days, {'panda_oidc_token': 2.0, 'bnl_rucio_proxy': 30.0})
        self.assertEqual(cc.superseded_days(results), {'evgen_output_proxy': -0.5})
        self.assertEqual(credential_verdict('warning', days, 1.0), (False, ''))

    def test_unavailable_token_and_expiring_proxy_is_green(self):
        from swf_epicprod.front import credential_verdict
        results = [
            {'credential': 'jlab_token', 'kind': 'token', 'status': 'unavailable'},
            {'credential': 'evgen_output_proxy', 'status': 'expiring', 'days_left': 6.7},
        ]
        self.assertEqual(credential_verdict('warning', cc.gate_days(results), 1.0), (False, ''))

    def test_a_stalled_token_probe_is_unavailable_at_its_deadline(self):
        # The probe's child process sleeps past the deadline: the entry is
        # unavailable in about the deadline, and the check carries on.
        with mock.patch.object(cc, '_PROBE', 'import time\ntime.sleep(30)  # %d\n'), \
                mock.patch.object(cc, 'JLAB_TOKEN_DEADLINE_S', 1):
            started = cc.time.monotonic()
            entry = cc._jlab_token()
            took = cc.time.monotonic() - started
        self.assertEqual(entry['status'], 'unavailable')
        self.assertIn('no answer in 1 s', entry['reason'])
        self.assertLess(took, 5)

    def test_the_deadline_leaves_the_proxies_their_time(self):
        # Two openssl reads at 15 s at most, inside the agent's 60 s.
        self.assertLessEqual(cc.JLAB_TOKEN_DEADLINE_S + 2 * 15, 55)


if __name__ == '__main__':
    unittest.main()
