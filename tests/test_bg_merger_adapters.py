"""Background merger adapters (docs/EPICPROD_PAYLOAD.md, Background
merging): each adapter's translation of the normalized arguments, and
run.sh's merge block over stub mergers. The legacy adapter must hand
SignalBackgroundMerger the command payload 0.23.1 built inline. No
container, no ROOT: the mergers, prmon and the frame count are stubs."""
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import unittest

PAYLOAD = os.path.join(os.path.dirname(__file__), '..', 'swf_epicprod', 'payload')
MERGERS = os.path.join(PAYLOAD, 'mergers')

SIGNAL = ('sig.hepmc3.tree.root', '0', '2000', '0')
BACKGROUNDS = [('root://door//a.hepmc3.tree.root', '36608000', '7321600001', '2000'),
               ('root://door//b.hepmc3.tree.root', '3177.25', '12345', '3000')]


def _stub(dirname, name, body):
    path = os.path.join(dirname, name)
    with open(path, 'w') as f:
        f.write('#!/bin/bash\n' + body + '\n')
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
    return path


def _normalized(signal=SIGNAL, backgrounds=BACKGROUNDS):
    args = ['--seed', '7', '--frames', '100', '--window', '2000',
            '--output', 'out.hepmc3.tree.root', '--signal', *signal]
    for bg in backgrounds:
        args += ['--background', *bg]
    return args


class AdapterBase(unittest.TestCase):
    def setUp(self):
        self.bin = tempfile.mkdtemp()
        self.argv = os.path.join(self.bin, 'argv')
        record = f'printf "%s\\n" "$@" > {self.argv}'
        _stub(self.bin, 'SignalBackgroundMerger', record)
        _stub(self.bin, 'timeframe_builder', record)
        self.env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}")

    def tearDown(self):
        shutil.rmtree(self.bin)

    def run_adapter(self, name, args):
        return subprocess.run(['bash', os.path.join(MERGERS, f'{name}.sh'), *args],
                              env=self.env, capture_output=True, text=True)

    def recorded(self):
        with open(self.argv) as f:
            return f.read().splitlines()


class Hepmcmerger(AdapterBase):
    def test_binary(self):
        self.assertEqual(self.run_adapter('hepmcmerger', ['--binary']).stdout.strip(),
                         'SignalBackgroundMerger')

    def test_the_0_23_1_command(self):
        self.assertEqual(self.run_adapter('hepmcmerger', _normalized()).returncode, 0)
        expected = ['--rngSeed', '7', '--nSlices', '100', '--signalSkip', '2000',
                    '--signalFile', 'sig.hepmc3.tree.root', '--signalFreq', '0',
                    '--signalStatus', '0', '--intWindow', '2000']
        for bg in BACKGROUNDS:
            expected += ['--bgFile', *bg]
        expected += ['--outputFile', 'out.hepmc3.tree.root']
        self.assertEqual(self.recorded(), expected)

    def test_merger_status_passes_through(self):
        # The adapter execs the merger, so its death is the adapter's.
        _stub(self.bin, 'SignalBackgroundMerger', 'kill -SEGV $$')
        self.assertEqual(self.run_adapter('hepmcmerger', _normalized()).returncode,
                         -signal.SIGSEGV)


class Timeframebuilder(AdapterBase):
    def test_binary(self):
        self.assertEqual(self.run_adapter('timeframebuilder', ['--binary']).stdout.strip(),
                         'timeframe_builder')

    def test_translation(self):
        self.assertEqual(self.run_adapter('timeframebuilder', _normalized()).returncode, 0)
        self.assertEqual(self.recorded(), [
            '--random-seed', '7', '--nevents', '100', '--duration', '2000',
            '--output', 'out.hepmc3.tree.root',
            '--source:signal:input_files', 'sig.hepmc3.tree.root',
            '--source:signal:static_events', 'true',
            '--source:signal:events_per_frame', '1',
            '--source:signal:skip', '2000',
            '--source:signal:status_offset', '0',
            '--source:signal:keep_weight', 'true',
            '--source:bg1:input_files', 'root://door//a.hepmc3.tree.root',
            '--source:bg1:frequency', '36.608',
            '--source:bg1:skip', '7321600001',
            '--source:bg1:status_offset', '2000',
            '--source:bg1:repeat_on_eof', 'true',
            '--source:bg2:input_files', 'root://door//b.hepmc3.tree.root',
            '--source:bg2:frequency', '0.00317725',
            '--source:bg2:skip', '12345',
            '--source:bg2:status_offset', '3000',
            '--source:bg2:repeat_on_eof', 'true'])

    def test_positive_signal_frequency(self):
        self.run_adapter('timeframebuilder', _normalized(signal=('s', '500', '0', '0')))
        argv = self.recorded()
        i = argv.index('--source:signal:frequency')
        self.assertEqual(argv[i + 1], '0.0005')
        self.assertNotIn('--source:signal:static_events', argv)

    def test_weighted_background_refused(self):
        done = self.run_adapter('timeframebuilder',
                                _normalized(backgrounds=[('w.hepmc3.tree.root', '0', '0', '0')]))
        self.assertEqual(done.returncode, 2)
        self.assertIn('weighted', done.stderr)
        self.assertFalse(os.path.exists(self.argv))


def _merge_block():
    """run.sh's merge block, from the background-merger default to the end
    of the hepmc branch."""
    with open(os.path.join(PAYLOAD, 'run.sh')) as f:
        text = f.read()
    start = text.index('# Background merger: hepmcmerger')
    end = text.index('echo "No background mixing is performed for singles"')
    return text[start:text.index('fi\n', end) + 3]


class MergeBlock(unittest.TestCase):
    """The block run in bash with the payload's helpers stubbed."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.bin = os.path.join(self.dir, 'bin')
        self.script_dir = os.path.join(self.dir, 'payload')
        os.makedirs(self.bin)
        shutil.copytree(MERGERS, os.path.join(self.script_dir, 'mergers'))
        # The frame count reads FRAMES_IN_FILE, set per test.
        with open(os.path.join(self.script_dir, 'count_events.py'), 'w') as f:
            f.write('import os, sys\n'
                    'v = os.environ.get("FRAMES_IN_FILE", "")\n'
                    'sys.exit(1) if not v else print(v)\n')
        self.argv = os.path.join(self.dir, 'argv')
        _stub(self.bin, 'SignalBackgroundMerger', f'printf "%s\\n" "$@" > {self.argv}')
        _stub(self.bin, 'eic-info', 'true')
        _stub(self.bin, 'python', f'exec {sys.executable} "$@"')
        _stub(self.bin, 'prmon', 'while [ "$1" != "--" ]; do shift; done; shift; exec "$@"')
        self.bg = os.path.join(self.dir, 'bg.json')
        with open(self.bg, 'w') as f:
            f.write('[{"file": "root://door//a.hepmc3.tree.root", "freq": 29.56, '
                    '"skip": 0.0, "status": 4000}]')
        self.stages = os.path.join(self.dir, 'stages')

    def tearDown(self):
        shutil.rmtree(self.dir)

    def run_block(self, **env):
        script = (
            'set -Euo pipefail\n'
            f'stage() {{ echo "$*" >> {self.stages}; }}\n'
            'guard_stage() { shift; "$@"; }\n'
            'REPORT_NOTE=""\n'
            f'SCRIPT_DIR={self.script_dir}; LOG_TEMP={self.dir}; FULL_TEMP={self.dir}\n'
            'TASKNAME=t; BASENAME=b; SEED=3; SKIP_N_EVENTS=200; EVENTS_PER_TASK=100\n'
            'INTEGRATION_WINDOW=2000\n'
            f'INPUT_FILE=sig.hepmc3.tree.root; EXTENSION=hepmc3.tree.root; BG_FILES={self.bg}\n'
            + _merge_block() +
            'echo "INPUT_FILE=${INPUT_FILE} SKIP=${SKIP_N_EVENTS}"\n')
        full_env = {k: v for k, v in os.environ.items() if k != 'BG_MERGER'}
        full_env.update(env, PATH=f"{self.bin}:{os.environ['PATH']}")
        return subprocess.run(['bash', '-c', script], env=full_env,
                              capture_output=True, text=True)

    def stage_lines(self):
        with open(self.stages) as f:
            return f.read().splitlines()

    def test_default_runs_the_legacy_merger(self):
        done = self.run_block(FRAMES_IN_FILE='100')
        self.assertEqual(done.returncode, 0, done.stderr)
        argv = self.recorded_argv()
        self.assertEqual(argv[:2], ['--rngSeed', '3'])
        self.assertIn('--bgFile', argv)
        self.assertIn(f'INPUT_FILE={self.dir}/t.hepmc3.tree.root SKIP=0', done.stdout)
        self.assertEqual(self.stage_lines(), ['background start hepmcmerger, 100 frames',
                                              'background ok hepmcmerger, 100 frames'])

    def test_short_merge_is_87(self):
        done = self.run_block(FRAMES_IN_FILE='50')
        self.assertEqual(done.returncode, 87)
        self.assertIn('50 frames of 100', self.stage_lines()[-1])

    def test_unreadable_merge_is_87(self):
        self.assertEqual(self.run_block(FRAMES_IN_FILE='').returncode, 87)

    def test_missing_binary_is_86(self):
        done = self.run_block(BG_MERGER='timeframebuilder', FRAMES_IN_FILE='100')
        self.assertEqual(done.returncode, 86)
        self.assertIn('timeframe_builder not in the image', self.stage_lines()[-1])

    def test_unknown_selector_is_86(self):
        done = self.run_block(BG_MERGER='nosuch', FRAMES_IN_FILE='100')
        self.assertEqual(done.returncode, 86)
        self.assertIn('unknown BG_MERGER nosuch', self.stage_lines()[-1])

    def recorded_argv(self):
        with open(self.argv) as f:
            return f.read().splitlines()


class LegacyIdentity(unittest.TestCase):
    """The 0.23.1 inline invocation, kept here as text, against the
    adapter: the same argument vector for the same inputs."""

    LEGACY = re.sub(r'\s+', ' ', '''
      SignalBackgroundMerger --rngSeed ${SEED:-1} --nSlices ${EVENTS_PER_TASK}
        --signalSkip ${SKIP_N_EVENTS} --signalFile ${INPUT_FILE}
        --signalFreq ${SIGNAL_FREQ:-0} --signalStatus ${SIGNAL_STATUS:-0}
        --intWindow ${INTEGRATION_WINDOW} "${BG_ARGS[@]}"
        --outputFile ${FULL_TEMP}/${TASKNAME}.hepmc3.tree.root''').strip()

    def test_same_vector(self):
        tmp = tempfile.mkdtemp()
        try:
            _stub(tmp, 'SignalBackgroundMerger', 'printf "%s\\n" "$@"')
            env = dict(os.environ, PATH=f"{tmp}:{os.environ['PATH']}")
            common = ('SEED=11; EVENTS_PER_TASK=100; SKIP_N_EVENTS=900; INPUT_FILE=s.root; '
                      'SIGNAL_FREQ=0; SIGNAL_STATUS=0; INTEGRATION_WINDOW=2000; '
                      'FULL_TEMP=/w; TASKNAME=t; ')
            legacy = subprocess.run(
                ['bash', '-c', common + 'BG_ARGS=(--bgFile f1 29.56 99 4000 --bgFile f2 1 2 3); '
                 + self.LEGACY], env=env, capture_output=True, text=True).stdout
            adapter = subprocess.run(
                ['bash', '-c', common + f'bash {MERGERS}/hepmcmerger.sh --seed ${{SEED:-1}} '
                 '--frames ${EVENTS_PER_TASK} --window ${INTEGRATION_WINDOW} '
                 '--output ${FULL_TEMP}/${TASKNAME}.hepmc3.tree.root '
                 '--signal ${INPUT_FILE} ${SIGNAL_FREQ:-0} ${SKIP_N_EVENTS} ${SIGNAL_STATUS:-0} '
                 '--background f1 29.56 99 4000 --background f2 1 2 3'],
                env=env, capture_output=True, text=True).stdout
            self.assertTrue(legacy)
            self.assertEqual(adapter, legacy)
        finally:
            shutil.rmtree(tmp)


if __name__ == '__main__':
    unittest.main()
