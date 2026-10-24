"""The scratch peak (EPICPROD_PAYLOAD.md, Scratch disk): with
EPICPROD_SWITCH_SCRATCH_PEAK=1 the stage marker logs the scratch's size at
every stage start and end; the report carries the peak and the job metrics
name it; the switch off writes nothing; a failing du never marks a stage."""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(__file__)
PAYLOAD = os.path.join(HERE, '..', 'swf_epicprod', 'payload')
sys.path.insert(0, PAYLOAD)
import payload_report as pr  # noqa: E402


def stage_function():
    text = open(os.path.join(PAYLOAD, 'run.sh')).read()
    m = re.search(r'(?ms)^stage\(\) \{\n.*?^\}\n', text)
    return m.group(0)


HARNESS = r'''
set -Euo pipefail
payload_report() { :; }
CURRENT_STAGE=""
%s
trap 's=$?; if [ -n "$CURRENT_STAGE" ]; then stage "$CURRENT_STAGE" fail "line $LINENO: $BASH_COMMAND"; fi; exit $s' ERR
stage simulation start
head -c 300000 /dev/zero > "$TMPDIR/full.root"
stage simulation ok
head -c 900000 /dev/zero > "$TMPDIR/reco.root"
stage reconstruction ok
rm -f "$TMPDIR/full.root"
stage closeout ok
'''


class ScratchPeak(unittest.TestCase):
    def run_stages(self, switch, failing_du=False):
        d = tempfile.mkdtemp()
        tmp = os.path.join(d, 'scratch'); os.mkdir(tmp)
        env = dict(os.environ, TMPDIR=tmp, PAYLOAD_STAGES_LOG=os.path.join(d, 'stages.log'),
                   PAYLOAD_SCRATCH_LOG=os.path.join(d, 'scratch.log'), TASKNAME='')
        if switch:
            env['EPICPROD_SWITCH_SCRATCH_PEAK'] = '1'
        if failing_du:
            # du as it is on an unreadable file: the total printed, exit 1
            bindir = os.path.join(d, 'bin'); os.mkdir(bindir)
            with open(os.path.join(bindir, 'du'), 'w') as f:
                f.write('#!/bin/sh\nprintf "4\\tx\\n"\nexit 1\n')
            os.chmod(os.path.join(bindir, 'du'), 0o755)
            env['PATH'] = bindir + os.pathsep + env['PATH']
        subprocess.run(['bash', '-c', HARNESS % stage_function()], env=env, check=True)
        return d

    def test_switch_on_logs_each_boundary_and_the_report_has_the_peak(self):
        d = self.run_stages(True)
        lines = open(os.path.join(d, 'scratch.log')).read().split('\n')
        self.assertEqual(len([l for l in lines if l]), 4)
        rec = pr.scratch_record(os.path.join(d, 'scratch.log'))
        self.assertEqual(rec['peak_stage'], 'reconstruction')
        self.assertGreaterEqual(rec['peak_kb'], 1100)
        self.assertEqual(rec['samples'], 4)
        self.assertLess(rec['by_stage_kb']['closeout'], rec['peak_kb'])
        # the scratch directory and two files at the end of reconstruction
        self.assertEqual(rec['peak_files'], 3)
        self.assertEqual(rec['peak_files_stage'], 'reconstruction')
        stages = open(os.path.join(d, 'stages.log')).read()
        self.assertNotIn(' fail', stages)

    def test_switch_off_writes_nothing(self):
        d = self.run_stages(False)
        self.assertFalse(os.path.exists(os.path.join(d, 'scratch.log')))
        self.assertIsNone(pr.scratch_record(os.path.join(d, 'scratch.log')))

    def test_a_failing_du_never_marks_a_stage_failed(self):
        d = self.run_stages(True, failing_du=True)
        stages = open(os.path.join(d, 'stages.log')).read()
        self.assertNotIn(' fail', stages)
        self.assertEqual(pr.scratch_record(os.path.join(d, 'scratch.log'))['peak_kb'], 4)

    def test_scratch_stays_in_the_report_not_the_capped_job_metrics(self):
        report = {'stages': {}, 'events': {}, 'registration': {'outcome': 'registered'},
                  'payload_version': 'epicprod-payload 0.25.2',
                  'scratch': {'peak_kb': 2167847, 'peak_stage': 'reconstruction', 'peak_files': 41}}
        metrics = pr.job_metrics(report)
        self.assertFalse(any(k.startswith('payloadScratch') for k in metrics))

    def test_malformed_lines_are_skipped(self):
        with tempfile.NamedTemporaryFile('w', delete=False) as f:
            f.write('garbage\n2026-10-24T09:00:00Z simulation ok 12\n2026-10-24T09:01:00Z x ok notanumber\n'
                    '2026-10-24T09:02:00Z y ok 10 -\n')
        rec = pr.scratch_record(f.name)
        self.assertEqual(rec['peak_kb'], 12)
        self.assertNotIn('peak_files', rec)


if __name__ == '__main__':
    unittest.main()


SELECT = re.compile(r'(?ms)^if \[ -n "\$\{_CONDOR_SCRATCH_DIR:-\}" \] ; then\n.*?^ls -al \$\{TMPDIR\}\n')
CLEANUP = re.compile(r'(?ms)^TMPDIR_OURS=0\ntmpdir_cleanup\(\) \{\n.*?^\}\n')


class OwnScratch(unittest.TestCase):
    """EPICPROD_SWITCH_OWN_SCRATCH: each job works in its own directory
    under the batch scratch and removes it at exit; the batch scratch and
    anything else in it stay."""

    def run_job(self, switch):
        text = open(os.path.join(PAYLOAD, 'run.sh')).read()
        d = tempfile.mkdtemp()
        batch = os.path.join(d, 'condor'); os.mkdir(batch)
        open(os.path.join(batch, 'pilot-file'), 'w').close()
        script = 'set -Euo pipefail\n' + CLEANUP.search(text).group(0) + SELECT.search(text).group(0) + '''
head -c 1000 /dev/zero > "$TMPDIR/full.root"
echo "$TMPDIR" > "%s/tmpdir"
tmpdir_cleanup
''' % d
        env = dict(os.environ, _CONDOR_SCRATCH_DIR=batch)
        if switch:
            env['EPICPROD_SWITCH_OWN_SCRATCH'] = '1'
        subprocess.run(['bash', '-c', script], env=env, check=True, stdout=subprocess.DEVNULL)
        return batch, open(os.path.join(d, 'tmpdir')).read().strip()

    def test_switch_on_removes_the_jobs_files_and_keeps_the_batch_scratch(self):
        batch, tmpdir = self.run_job(True)
        self.assertTrue(tmpdir.startswith(batch + '/epicprod-payload-'))
        self.assertFalse(os.path.exists(tmpdir))
        self.assertEqual(os.listdir(batch), ['pilot-file'])

    def test_switch_off_leaves_the_files_as_before(self):
        batch, tmpdir = self.run_job(False)
        self.assertEqual(tmpdir, batch)
        self.assertIn('full.root', os.listdir(batch))
