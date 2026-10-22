"""The front's credential gate over the nightly check's record
(swf_epicprod.front.credential_verdict is pure)."""
import unittest

from swf_epicprod.front import CREDENTIAL_STALE_H, credential_verdict

DAYS = {'panda_oidc_token': 20.0, 'bnl_rucio_proxy': 348.4, 'evgen_output_proxy': 6.72}


class CredentialVerdictTest(unittest.TestCase):
    def test_ok_is_green(self):
        self.assertEqual(credential_verdict('ok', DAYS, 5.0), (False, ''))

    def test_inside_the_window_is_green(self):
        self.assertEqual(credential_verdict('warning', DAYS, 5.0), (False, ''))

    def test_unset_credential_is_red(self):
        red, reason = credential_verdict('warning', dict(DAYS, evgen_output_proxy=None), 5.0)
        self.assertTrue(red)
        self.assertIn('warning', reason)

    def test_warning_without_days_is_red(self):
        self.assertTrue(credential_verdict('warning', None, 5.0)[0])
        self.assertTrue(credential_verdict('warning', {}, 5.0)[0])

    def test_expired_is_red(self):
        self.assertTrue(credential_verdict('warning', dict(DAYS, evgen_output_proxy=-0.5), 5.0)[0])
        self.assertTrue(credential_verdict('error', dict(DAYS, evgen_output_proxy=-0.5), 5.0)[0])

    def test_timeout_and_unrecorded_are_red(self):
        self.assertTrue(credential_verdict('timeout', None, 5.0)[0])
        self.assertEqual(credential_verdict('', None, 5.0), (True, 'credential check unrecorded'))

    def test_stale_check_is_red_whatever_it_said(self):
        red, reason = credential_verdict('ok', DAYS, CREDENTIAL_STALE_H + 1)
        self.assertTrue(red)
        self.assertIn('h old', reason)


if __name__ == '__main__':
    unittest.main()
