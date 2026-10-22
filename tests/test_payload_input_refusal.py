"""run.sh's input refusal: a simulation that dies on a signal after ROOT
reports the door's refusal of its input exits 88 under
EPICPROD_SWITCH_INPUT_REFUSAL=1, and with the signal's status otherwise
(EPICPROD_PAYLOAD.md, Exit codes). The function and the ERR trap are
taken from run.sh itself and run in bash with a stand-in stage."""
import os
import re
import subprocess
import tempfile
import unittest

RUN_SH = os.path.join(os.path.dirname(__file__), '..', 'swf_epicprod', 'payload', 'run.sh')
REFUSAL = ('Error in <TNetXNGFile::Open>: [ERROR] Server responded with an error: '
           '[3005] Unable to open /jlab-osdf-ro/eic/EPIC/volatile/EVGEN/x.hepmc3.tree.root; '
           'no such device')


def _parts():
    text = open(RUN_SH).read()
    regex = re.search(r"^INPUT_REFUSED_RE=.*$", text, re.M).group(0)
    func = re.search(r"^input_refusal\(\) \{.*?^\}$", text, re.M | re.S).group(0)
    trap = re.search(r"^trap 's=\$\?; echo .*' ERR$", text, re.M).group(0)
    return regex, func, trap


def run(log_text, switch, stage='simulation', status=139):
    regex, func, trap = _parts()
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, 'T.npsim.log'), 'w') as f:
            f.write(log_text)
        script = '\n'.join([
            'set -Euo pipefail',
            f'LOG_TEMP={tmp}; TASKNAME=T; CURRENT_STAGE={stage}; REPORT_NOTE=crash-tail',
            'stage() { :; }',
            'crash_capture() { trap - ERR; }',
            regex, func, trap,
            f'bash -c "exit {status}"',
            'echo unreachable',
        ])
        env = dict(os.environ)
        env.pop('EPICPROD_SWITCH_INPUT_REFUSAL', None)
        if switch:
            env['EPICPROD_SWITCH_INPUT_REFUSAL'] = '1'
        p = subprocess.run(['bash', '-c', script], capture_output=True, text=True, env=env)
        return p.returncode, p.stdout


class InputRefusalTest(unittest.TestCase):
    def test_refusal_with_the_switch_is_88(self):
        code, out = run('geometry...\n' + REFUSAL + '\n[FATAL] (SignalHandler) Handle signal: 11\n', True)
        self.assertEqual(code, 88)
        self.assertIn('exiting 88', out)
        self.assertNotIn('unreachable', out)

    def test_refusal_without_the_switch_keeps_the_signal(self):
        self.assertEqual(run(REFUSAL + '\n', False)[0], 139)

    def test_a_crash_without_the_refusal_keeps_the_signal(self):
        self.assertEqual(run('[FATAL] (SignalHandler) Handle signal: 11\n', True)[0], 139)

    def test_only_the_simulation_stage(self):
        self.assertEqual(run(REFUSAL + '\n', True, stage='reconstruction')[0], 139)

    def test_a_plain_failure_is_untouched(self):
        self.assertEqual(run(REFUSAL + '\n', True, status=1)[0], 1)


if __name__ == '__main__':
    unittest.main()
