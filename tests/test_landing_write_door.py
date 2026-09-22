"""The write-door landing check (docs/EPICPROD_PAYLOAD.md, exit code 86):
the decision functions over their inputs, and the check itself over a
fake door and a fake openssl. No network, no door."""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'swf_epicprod', 'payload'))
import landing_check as lc  # noqa: E402

NOW = datetime(2026, 9, 22, 17, 0, tzinfo=timezone.utc)
EXPIRED = NOW - timedelta(days=2)
VALID = NOW + timedelta(days=133)

# The answer the door with the expired certificate actually gave
# (job 3558625, 2026-09-22).
TLS_TEXT = ('[FATAL] TLS error: resource temporarily unavailable: Unable to '
            'connect to epicxrd1.sdcc.bnl.gov; error_ssl (destination)')


class AnswerClass(unittest.TestCase):
    def test_success_is_ok(self):
        self.assertEqual(lc.door_answer_class(0, 'Path: /\nSize: 0\n'), 'ok')

    def test_the_observed_tls_refusal(self):
        self.assertEqual(lc.door_answer_class(51, TLS_TEXT), 'tls')

    def test_timeout_is_not_tls(self):
        self.assertEqual(lc.door_answer_class(124, 'no answer in 30s'), 'other')

    def test_missing_path_is_not_tls(self):
        self.assertEqual(lc.door_answer_class(54, '[ERROR] No such file or directory'),
                         'other')

    def test_absent_client_is_not_tls(self):
        self.assertEqual(lc.door_answer_class(127, 'FileNotFoundError: xrdfs'), 'other')


class CertificateExpiry(unittest.TestCase):
    def test_reads_the_openssl_line(self):
        read = lc.certificate_expiry('notAfter=Sep 20 23:59:59 2026 GMT\n')
        self.assertEqual(read, datetime(2026, 9, 20, 23, 59, 59, tzinfo=timezone.utc))

    def test_without_the_zone(self):
        read = lc.certificate_expiry('notAfter=Feb  2 12:00:00 2027')
        self.assertEqual(read, datetime(2027, 2, 2, 12, 0, tzinfo=timezone.utc))

    def test_no_line_is_none(self):
        self.assertIsNone(lc.certificate_expiry('unable to load certificate'))

    def test_unreadable_date_is_none(self):
        self.assertIsNone(lc.certificate_expiry('notAfter=whenever'))

    def test_empty_is_none(self):
        self.assertIsNone(lc.certificate_expiry(''))


class Verdict(unittest.TestCase):
    """The certificate decides, not the way the client complained:
    nothing short of a date read and past refuses a landing, because
    declining spends an attempt."""

    def test_no_answer_and_an_expired_certificate_refuses(self):
        self.assertEqual(lc.write_door_verdict(False, EXPIRED, NOW), 'refused')

    def test_no_answer_but_the_certificate_stands(self):
        self.assertEqual(lc.write_door_verdict(False, VALID, NOW), 'proceed')

    def test_no_answer_and_no_certificate_read(self):
        self.assertEqual(lc.write_door_verdict(False, None, NOW), 'proceed')

    def test_a_door_that_answers_proceeds_whatever_the_date(self):
        self.assertEqual(lc.write_door_verdict(True, EXPIRED, NOW), 'proceed')

    def test_expiry_in_the_same_second_stands(self):
        self.assertEqual(lc.write_door_verdict(False, NOW, NOW), 'refused')


class CheckWriteDoor(unittest.TestCase):
    """The check over a fake door: xrdfs answers and an openssl pair."""

    def _run_with(self, stat_answers, enddate=None, chain=True):
        """A _run that answers xrdfs from a list and openssl from the
        certificate arguments."""
        stats = list(stat_answers)

        def run(command, timeout, stdin_text=None):
            if command[0] == 'xrdfs':
                return stats.pop(0)
            if command[1] == 's_client':
                if not chain:
                    return 1, 'connect: errno=111'
                return 0, ('CONNECTED\n-----BEGIN CERTIFICATE-----\nMIIB\n'
                           '-----END CERTIFICATE-----\n')
            if enddate is None:
                return 1, 'unable to load certificate'
            return 0, f'notAfter={enddate}\n'
        return run

    def test_no_door_is_not_checked(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('LANDING_WRITE_DOOR', None)
            self.assertTrue(lc.check_write_door())

    def test_a_door_without_a_host_is_not_checked(self):
        with mock.patch.dict(os.environ, {'LANDING_WRITE_DOOR': 'root://'}):
            self.assertTrue(lc.check_write_door())

    def test_an_answering_door_proceeds_without_reading_the_certificate(self):
        run = self._run_with([(0, 'Path: /\nSize: 0\n')], enddate='Sep 20 23:59:59 2026 GMT')
        with mock.patch.dict(os.environ, {'LANDING_WRITE_DOOR': 'root://door:1094'}), \
                mock.patch.object(lc, '_run', run):
            self.assertTrue(lc.check_write_door())

    def test_a_tls_refusal_with_an_expired_certificate_declines(self):
        run = self._run_with([(51, TLS_TEXT)], enddate='Sep 20 23:59:59 2026 GMT')
        with mock.patch.dict(os.environ, {'LANDING_WRITE_DOOR': 'root://door:1094'}), \
                mock.patch.object(lc, '_run', run):
            self.assertFalse(lc.check_write_door())

    def test_a_timeout_with_an_expired_certificate_declines(self):
        """The client's complaint does not matter: from a worker without
        a proxy the same dead door times out rather than naming TLS."""
        run = self._run_with([(124, 'no answer in 30s')], enddate='Sep 20 23:59:59 2026 GMT')
        with mock.patch.dict(os.environ, {'LANDING_WRITE_DOOR': 'root://door:1094'}), \
                mock.patch.object(lc, '_run', run):
            self.assertFalse(lc.check_write_door())

    def test_a_refusal_with_a_certificate_in_force_proceeds(self):
        run = self._run_with([(51, TLS_TEXT)], enddate='Feb  2 12:00:00 2027 GMT')
        with mock.patch.dict(os.environ, {'LANDING_WRITE_DOOR': 'root://door:1094'}), \
                mock.patch.object(lc, '_run', run):
            self.assertTrue(lc.check_write_door())

    def test_a_refusal_with_no_certificate_read_proceeds(self):
        run = self._run_with([(51, TLS_TEXT)], chain=False)
        with mock.patch.dict(os.environ, {'LANDING_WRITE_DOOR': 'root://door:1094'}), \
                mock.patch.object(lc, '_run', run):
            self.assertTrue(lc.check_write_door())

    def test_an_unreachable_door_with_no_certificate_proceeds(self):
        run = self._run_with([(124, 'no answer in 30s')], chain=False)
        with mock.patch.dict(os.environ, {'LANDING_WRITE_DOOR': 'root://door:1094'}), \
                mock.patch.object(lc, '_run', run):
            self.assertTrue(lc.check_write_door())


class MainExitCodes(unittest.TestCase):
    """The write door is its own code: a worker-local negative still
    reports 4, and a clean landing with a dead door reports 5."""

    def _main_with(self, door_ok, catalog_ok=True):
        with mock.patch.dict(os.environ, {'RUCIO_CONFIG': '', 'XRDRURL': ''}), \
                mock.patch.object(lc, 'check_exclusion', return_value=catalog_ok), \
                mock.patch.object(lc, 'check_write_door', return_value=door_ok):
            return lc.main()

    def test_everything_well(self):
        self.assertEqual(self._main_with(door_ok=True), 0)

    def test_the_dead_door_alone(self):
        self.assertEqual(self._main_with(door_ok=False), 5)

    def test_a_worker_local_negative_comes_first(self):
        self.assertEqual(self._main_with(door_ok=False, catalog_ok=False), 4)


if __name__ == '__main__':
    unittest.main()
