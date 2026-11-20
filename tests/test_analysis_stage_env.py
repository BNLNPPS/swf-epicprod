"""A card's stage runs without the payload's credentials
(swf_epicprod/payload/analysis_job.py, docs/ANALYSIS.md, The job): it sees
the job's environment less the production Rucio config, tokens and proxy,
and less the payload's patched Rucio client. No PanDA, no Rucio."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'swf_epicprod', 'payload'))
import analysis_job  # noqa: E402

JOB_ENV = {
    'RUCIO_CONFIG': '/job/rucio-token.cfg', 'RUCIO_ACCOUNT': 'eicprod',
    'BEARER_TOKEN': 'tok', 'BEARER_TOKEN_FILE': '/job/bt',
    'X509_USER_PROXY': '/job/x509up', 'PILOT_X509_USER_PROXY': '/job/x509up',
    'OIDC_AUTH_TOKEN': 'panda', 'PANDA_AUTH_TOKEN': 'panda',
    'X509_CERT_DIR': '/cvmfs/certs', 'PATH': '/usr/bin', 'HOME': '/job',
    'PYTHONPATH': '/job/rucio-clients/lib:/opt/local/python',
}
CARD = {'stages': {'map': 'snakemake m'}}


class StageEnvironmentTest(unittest.TestCase):

    def stage_env(self, values=None):
        with mock.patch.dict(os.environ, JOB_ENV, clear=True), \
                mock.patch.object(analysis_job.subprocess, 'run') as run:
            run.return_value.returncode = 0
            analysis_job.run_stage(CARD, 'map', '/job/repo', values or {'SAMPLE': 's', 'JOB': '1'})
        return run.call_args.kwargs['env']

    def test_payload_credentials_are_hidden(self):
        env = self.stage_env()
        for name in ('RUCIO_CONFIG', 'RUCIO_ACCOUNT', 'BEARER_TOKEN', 'BEARER_TOKEN_FILE',
                     'X509_USER_PROXY', 'PILOT_X509_USER_PROXY', 'OIDC_AUTH_TOKEN', 'PANDA_AUTH_TOKEN'):
            self.assertNotIn(name, env)

    def test_the_rest_of_the_job_environment_and_the_values_stay(self):
        env = self.stage_env()
        self.assertEqual((env['X509_CERT_DIR'], env['PATH'], env['HOME']), ('/cvmfs/certs', '/usr/bin', '/job'))
        self.assertEqual((env['SAMPLE'], env['JOB']), ('s', '1'))

    def test_patched_rucio_client_leaves_pythonpath(self):
        self.assertEqual(self.stage_env()['PYTHONPATH'], '/opt/local/python')

    def test_a_failing_stage_fails_the_job(self):
        with mock.patch.dict(os.environ, JOB_ENV, clear=True), \
                mock.patch.object(analysis_job.subprocess, 'run') as run:
            run.return_value.returncode = 2
            with self.assertRaises(analysis_job.JobError):
                analysis_job.run_stage(CARD, 'map', '/job/repo', {})

    def test_payload_steps_keep_their_credentials(self):
        with mock.patch.dict(os.environ, JOB_ENV, clear=True), \
                mock.patch.object(analysis_job.subprocess, 'run') as run:
            run.return_value.returncode = 0
            analysis_job.run(['true'])
        self.assertEqual(run.call_args.kwargs['env']['RUCIO_CONFIG'], '/job/rucio-token.cfg')


class ReportInLogTest(unittest.TestCase):

    def test_report_is_logged_on_one_line(self):
        import io, json, tempfile
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {'ANALYSIS_STAGE': 'map', 'ANALYSIS_RUN_ID': 'r' * 64}, clear=True), \
                mock.patch.object(analysis_job, 'REPORT', os.path.join(tmp, 'report.json')), \
                mock.patch.object(sys, 'argv', ['analysis_job.py', '1', os.path.join(tmp, 'none')]), \
                mock.patch('sys.stderr', new_callable=io.StringIO) as err, \
                mock.patch('sys.stdout', new_callable=io.StringIO) as out:
            self.assertEqual(analysis_job.main(), 1)
        lines = [l for l in (out.getvalue() + err.getvalue()).splitlines() if 'report: ' in l]
        self.assertEqual(len(lines), 1)
        report = json.loads(lines[0].split('report: ', 1)[1])
        self.assertEqual((report['stage'], report['exit']), ('map', 1))


if __name__ == '__main__':
    unittest.main()
