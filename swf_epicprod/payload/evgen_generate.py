#!/usr/bin/env python3
"""evgen_generate.py — the payload's internal EVGEN stage
(docs/EPICPROD_INTERNAL_EVGEN.md).

Generates the sample this job simulates, at the path an externally
supplied sample would have had, from the generation environment the
submission wrote (EVGEN_GENERATOR, EVGEN_PROCESS, EVGEN_Q2_RANGE or
EVGEN_STATE/EVGEN_MECHANISM, EVGEN_BEAM_SPECIES, EVGEN_RADIATIVE, EBEAM,
PBEAM, EVGEN_AB_PRESET):

1. composes the generator's steering file from that environment, one
   composer per (generator, process) pair,
2. runs the generator to HepMC3 ASCII: pythia8 through the driver
   (evgen_pythia8_hepmc3.cc) compiled against the campaign image's
   Pythia and HepMC3, once per job; eSTARlight as the image's own
   e_starlight,
3. runs the afterburner (abconv) on the result: beam effects applied,
   written as hepmc3.tree.root,
4. moves the file to --out and writes a summary beside it.

Everything the stage did is in its log; every failure names its step.
Exit 0 with the file in place; nonzero otherwise, with the reason as the
last ERROR line. Standard library only, run under the image's Python.

Usage::

    evgen_generate.py --out EVGEN/<path>/<sample>_run007.hepmc3.tree.root \\
        --events 100 --workdir /tmp/evgen --summary evgen-summary.json
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DRIVER_SOURCE = os.path.join(HERE, 'evgen_pythia8_hepmc3.cc')
RUN_RE = re.compile(r'_run(\d+)(?:\.|$)')


def fail(msg, code=1):
    print(f'ERROR: {msg}', file=sys.stderr)
    sys.exit(code)


def env_value(name, default=None, required=False):
    value = os.environ.get(name, '').strip()
    if value:
        return value
    if required:
        fail(f'{name} is not set in the job environment')
    return default


def q2_bounds(q2_range):
    """('10', '100') from 'q2_10to100'; the upper bound None for INF."""
    m = re.fullmatch(r'q2_(\d+(?:\.\d+)?)to(\d+(?:\.\d+)?|INF)', q2_range or '',
                     re.IGNORECASE)
    if not m:
        fail(f'EVGEN_Q2_RANGE {q2_range!r} is not of the form q2_<lo>to<hi>')
    lo, hi = m.group(1), m.group(2)
    return lo, (None if hi.upper() == 'INF' else hi)


HADRON_IDS = {'p': 2212, 'n': 2112, 'd': 1000010020, 'he3': 1000020030}


def hadron_id(species):
    """The hadron beam's PDG id from the species token: 'ep' -> proton."""
    token = (species or 'ep').lower()
    if not token.startswith('e'):
        fail(f'EVGEN_BEAM_SPECIES {species!r} does not name a lepton-hadron '
             'collision (expected e<hadron>, e.g. ep)')
    hadron = token[1:] or 'p'
    if hadron in HADRON_IDS:
        return HADRON_IDS[hadron]
    fail(f'no beam id for hadron species {hadron!r} (known: '
         f'{sorted(HADRON_IDS)})')


def pythia8_dis_nc_card(events, seed):
    """Neutral-current DIS in Pythia 8: the steering stated in
    docs/EPICPROD_INTERNAL_EVGEN.md § The steering. Hadron along +z as
    beam A, electron along -z as beam B, the frame the existing samples
    use; radiative corrections off means no QED radiation from the
    lepton (no lepton PDF, no QED shower off leptons)."""
    ebeam = env_value('EBEAM', required=True)
    pbeam = env_value('PBEAM', required=True)
    lo, hi = q2_bounds(env_value('EVGEN_Q2_RANGE', required=True))
    radiative = env_value('EVGEN_RADIATIVE', 'off').lower()
    lines = [
        '! epicprod internal EVGEN: pythia8 neutral-current DIS',
        '! composed by evgen_generate.py from the job environment',
        'Beams:frameType = 2',
        f'Beams:idA = {hadron_id(env_value("EVGEN_BEAM_SPECIES", "ep"))}',
        f'Beams:eA = {pbeam}',
        'Beams:idB = 11',
        f'Beams:eB = {ebeam}',
        'WeakBosonExchange:ff2ff(t:gmZ) = on',
        f'PhaseSpace:Q2Min = {lo}',
    ]
    if hi is not None:
        lines.append(f'PhaseSpace:Q2Max = {hi}')
    # Radiative corrections off is the lepton without a PDF: no initial-
    # state QED radiation from the electron. Final-state QED showering
    # stays at Pythia's default (on): with it off, 3000 generated 10x100
    # events fell 3 per cent below the registered sample in final-state
    # multiplicity, and with it on they matched (2026-09-07).
    if radiative == 'off':
        lines.append('PDF:lepton = off')
    elif radiative != 'on':
        fail(f'EVGEN_RADIATIVE {radiative!r}: expected on or off')
    lines += [
        'SpaceShower:dipoleRecoil = on',
        'Random:setSeed = on',
        f'Random:seed = {seed}',
        f'Main:numberOfEvents = {events}',
        'Main:timesAllowErrors = 10',
        'Next:numberCount = 1000',
        'Next:numberShowEvent = 0',
    ]
    return '\n'.join(lines) + '\n'


ELECTRON_MASS = 0.000510998950   # GeV
PROTON_MASS = 0.93827208816      # GeV
# eSTARlight's PROD_PID for the Upsilon states and their masses (GeV),
# the mass setting the gamma-p energy window's lower edge.
UPSILON_STATES = {'1s': (553, 9.4603), '2s': (100553, 10.0233),
                  '3s': (200553, 10.3552)}
TARGET_ZA = {'p': (1, 1)}


def estarlight_upsilon_card(events, seed):
    """Exclusive Upsilon photoproduction in eSTARlight, e p -> e p Upsilon:
    production mode 12 (the narrow-resonance photon-Pomeron channel), the
    state from EVGEN_STATE, the steering stated in
    docs/EPICPROD_INTERNAL_EVGEN.md § The steering. eSTARlight takes the
    beams as Lorentz factors and writes the electron along -z and the
    hadron along +z, the frame the afterburner expects; OUTPUT_FORMAT 2
    is HepMC3 ASCII (slight.hepmc). The kinematic windows are the
    generator's example values; the gamma-p energy window runs from just
    above the state's threshold to just below the collision energy."""
    ebeam = float(env_value('EBEAM', required=True))
    pbeam = float(env_value('PBEAM', required=True))
    state = env_value('EVGEN_STATE', required=True).lower()
    mechanism = env_value('EVGEN_MECHANISM', 'photo').lower()
    if state not in UPSILON_STATES:
        fail(f'EVGEN_STATE {state!r}: the eSTARlight Upsilon states are '
             f'{sorted(UPSILON_STATES)}')
    if mechanism != 'photo':
        fail(f'EVGEN_MECHANISM {mechanism!r}: only photoproduction (photo) '
             'is composed for eSTARlight')
    hadron = env_value('EVGEN_BEAM_SPECIES', 'ep').lower()[1:] or 'p'
    if hadron not in TARGET_ZA:
        fail(f'no eSTARlight target for hadron species {hadron!r} (known: '
             f'{sorted(TARGET_ZA)})')
    z, a = TARGET_ZA[hadron]
    pid, mass = UPSILON_STATES[state]
    roots = 2.0 * (ebeam * pbeam) ** 0.5
    w_gp_min = round(mass + PROTON_MASS + 0.1, 2)
    w_gp_max = round(0.99 * roots, 1)
    if w_gp_max <= w_gp_min:
        fail(f'no gamma-p energy window for Upsilon({state}) at {ebeam}x{pbeam}: '
             f'threshold {w_gp_min} GeV, collision energy {roots:.1f} GeV')
    lines = [
        '# epicprod internal EVGEN: eSTARlight exclusive Upsilon photoproduction',
        '# composed by evgen_generate.py from the job environment',
        'baseFileName = slight',
        f'TARGET_BEAM_Z = {z}',
        f'TARGET_BEAM_A = {a}',
        f'ELECTRON_BEAM_GAMMA = {ebeam / ELECTRON_MASS:.4f}',
        f'TARGET_BEAM_GAMMA = {pbeam / PROTON_MASS:.4f}',
        'W_MAX = -1',
        'W_MIN = -1',
        'W_N_BINS = 50',
        f'W_GP_MIN = {w_gp_min}',
        f'W_GP_MAX = {w_gp_max}',
        'RAP_MAX = 9.',
        'RAP_N_BINS = 200',
        'EGA_N_BINS = 400',
        'CUT_PT = 0',
        'PT_MIN = 1.0',
        'PT_MAX = 3.0',
        'CUT_ETA = 0',
        'ETA_MIN = -10',
        'ETA_MAX = 10',
        'PROD_MODE = 12',
        f'N_EVENTS = {events}',
        f'PROD_PID = {pid}',
        f'RND_SEED = {seed}',
        'BREAKUP_MODE = 5',
        'INTERFERENCE = 0',
        'IF_STRENGTH = 1.',
        'INT_PT_MAX = 0.24',
        'INT_PT_N_BINS = 120',
        'MIN_GAMMA_ENERGY = 6.0',
        'MAX_GAMMA_ENERGY = 600000.0',
        'MIN_GAMMA_Q2 = 0.',
        'MAX_GAMMA_Q2 = 100.',
        'INT_GAMMA_Q2_BINS = 400',
        'OUTPUT_FORMAT = 2',
    ]
    return '\n'.join(lines) + '\n'


CARDS = {('pythia8', 'DIS_NC'): pythia8_dis_nc_card,
         ('estarlight', 'UPSILON'): estarlight_upsilon_card}
CARD_SUFFIX = {'pythia8': '.cmnd', 'estarlight': '.in'}


def run(cmd, log, **kwargs):
    """Run a command, its output appended to the stage log and echoed."""
    print('+ ' + ' '.join(cmd), flush=True)
    with open(log, 'a') as handle:
        handle.write('+ ' + ' '.join(cmd) + '\n')
        handle.flush()
        proc = subprocess.run(cmd, stdout=handle, stderr=subprocess.STDOUT,
                              text=True, **kwargs)
    return proc.returncode


def tail(path, lines=20):
    try:
        with open(path, errors='replace') as handle:
            return ''.join(handle.readlines()[-lines:])
    except OSError:
        return ''


def flags(tool, *args):
    try:
        out = subprocess.run([tool, *args], capture_output=True, text=True,
                             check=True).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        fail(f'{tool} {" ".join(args)} failed: {exc}')
    return out.split()


def compile_driver(workdir, log):
    """The driver binary in workdir, built against the image's Pythia and
    HepMC3. A few seconds; once per job."""
    binary = os.path.join(workdir, 'evgen_pythia8_hepmc3')
    cmd = ['g++', '-O2', '-std=c++17', '-o', binary, DRIVER_SOURCE]
    cmd += flags('pythia8-config', '--cxxflags')
    cmd += flags('HepMC3-config', '--cxxflags')
    cmd += flags('pythia8-config', '--libs')
    cmd += flags('HepMC3-config', '--libs')
    if run(cmd, log) != 0:
        fail('driver compilation failed:\n' + tail(log))
    return binary


def generate_pythia8(card_path, ascii_path, workdir, log):
    """The requested events to HepMC3 ASCII through the compiled driver."""
    binary = compile_driver(workdir, log)
    if run([binary, card_path, ascii_path], log) != 0:
        fail('generation failed:\n' + tail(log))


def generate_estarlight(card_path, ascii_path, workdir, log):
    """The requested events to HepMC3 ASCII through the image's
    e_starlight, which reads slight.in from its working directory and
    writes slight.hepmc beside it (OUTPUT_FORMAT 2) with its text
    outputs."""
    rundir = os.path.join(workdir, 'estarlight')
    os.makedirs(rundir, exist_ok=True)
    shutil.copyfile(card_path, os.path.join(rundir, 'slight.in'))
    if run(['e_starlight'], log, cwd=rundir) != 0:
        fail('eSTARlight failed:\n' + tail(log))
    produced = os.path.join(rundir, 'slight.hepmc')
    if not os.path.exists(produced) or os.path.getsize(produced) == 0:
        fail(f'eSTARlight wrote no events to {produced}:\n' + tail(log))
    shutil.move(produced, ascii_path)


GENERATORS = {'pythia8': generate_pythia8, 'estarlight': generate_estarlight}


AB_SOURCE = os.path.join(HERE, 'afterburner-cpp.tgz')
AB_SOURCE_VERSION = os.path.join(HERE, 'afterburner-cpp.VERSION')


def afterburner_binary(workdir, log, preset):
    """The abconv to run: the image's own for a numbered preset, else the
    shipped afterburner source built in the job. The campaign images
    carry afterburner 0.1.3, whose configurations stop at the five
    nominal ep energies; the repository's current source knows the 9 GeV
    electron configurations, so a named preset (ip6_ep_130x9 and the
    like), or EVGEN_AB_BUILD=true, builds it here: a few minutes on one
    core, once per job (docs/EPICPROD_INTERNAL_EVGEN.md)."""
    build = env_value('EVGEN_AB_BUILD', '').lower() in ('1', 'true', 'yes')
    if not build and not preset.isdigit():
        build = True
    if not build:
        return 'abconv', 'image'
    if not os.path.exists(AB_SOURCE):
        fail(f'the afterburner source {AB_SOURCE} is not in the payload')
    src = os.path.join(workdir, 'afterburner-src')
    bld = os.path.join(workdir, 'afterburner-build')
    os.makedirs(src, exist_ok=True)
    if run(['tar', 'xzf', AB_SOURCE, '-C', src], log) != 0:
        fail('could not unpack the afterburner source:\n' + tail(log))
    jobs = str(max(1, min(4, os.cpu_count() or 1)))
    if run(['cmake', '-S', os.path.join(src, 'cpp'), '-B', bld,
            '-DCMAKE_BUILD_TYPE=Release'], log) != 0:
        fail('afterburner cmake configuration failed:\n' + tail(log))
    if run(['cmake', '--build', bld, '-j', jobs, '--target', 'abconv'], log) != 0:
        fail('afterburner build failed:\n' + tail(log))
    for root, _dirs, files in os.walk(bld):
        if 'abconv' in files:
            binary = os.path.join(root, 'abconv')
            if os.access(binary, os.X_OK):
                version = tail(AB_SOURCE_VERSION, 1).strip() or 'shipped source'
                return binary, f'sandbox build of {version}'
    fail('the afterburner build produced no abconv binary')


def count_tree_events(path):
    """Entries of the hepmc3_tree in a treeroot file, via ROOT when it is
    importable; None otherwise (the count is then abconv's own)."""
    try:
        import ROOT  # noqa: WPS433 — the image's PyROOT
    except ImportError:
        return None
    ROOT.gErrorIgnoreLevel = ROOT.kError
    tfile = ROOT.TFile.Open(path)
    if not tfile or tfile.IsZombie():
        return None
    tree = tfile.Get('hepmc3_tree')
    return int(tree.GetEntries()) if tree else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', required=True,
                    help='destination path of the generated hepmc3.tree.root')
    ap.add_argument('--events', type=int, required=True)
    ap.add_argument('--seed', type=int, default=0,
                    help='Pythia seed; 0 derives run index + 1 from the '
                         'output name (_run<NNN>)')
    ap.add_argument('--workdir', required=True,
                    help='scratch directory for the card, driver and files')
    ap.add_argument('--log', default='',
                    help='stage log file (default <workdir>/evgen.log)')
    ap.add_argument('--summary', default='',
                    help='summary JSON to write (default <workdir>/evgen-summary.json)')
    args = ap.parse_args()

    started = time.time()
    os.makedirs(args.workdir, exist_ok=True)
    log = args.log or os.path.join(args.workdir, 'evgen.log')
    summary_path = args.summary or os.path.join(args.workdir, 'evgen-summary.json')

    generator = env_value('EVGEN_GENERATOR', required=True).lower()
    process = env_value('EVGEN_PROCESS', required=True).upper()
    composer = CARDS.get((generator, process))
    if composer is None:
        fail(f'no steering composer for generator {generator!r} and process '
             f'{process!r} (known: {sorted(CARDS)})')
    generate = GENERATORS.get(generator)
    if generate is None:
        fail(f'no runner for generator {generator!r} (known: {sorted(GENERATORS)})')

    seed = args.seed
    if seed <= 0:
        m = RUN_RE.search(os.path.basename(args.out))
        if not m:
            fail('no --seed and no _run<NNN> in the output name to derive it from')
        seed = int(m.group(1)) + 1
    if args.events <= 0:
        fail(f'--events must be positive, got {args.events}')

    stem = os.path.basename(args.out)
    for suffix in ('.hepmc3.tree.root', '.hepmc3.root', '.root'):
        if stem.endswith(suffix):
            stem = stem[:-len(suffix)]
            break
    card_path = os.path.join(args.workdir, stem + CARD_SUFFIX.get(generator, '.card'))
    ascii_path = os.path.join(args.workdir, f'{stem}.hepmc')
    with open(card_path, 'w') as handle:
        handle.write(composer(args.events, seed))
    print(f'steering ({card_path}):')
    print(open(card_path).read())

    t0 = time.time()
    generate(card_path, ascii_path, args.workdir, log)
    generation_s = round(time.time() - t0, 1)
    if not os.path.exists(ascii_path) or os.path.getsize(ascii_path) == 0:
        fail(f'the driver wrote no events to {ascii_path}')

    # The afterburner preset: 0 is IP6 high divergence, abconv's own
    # default and what the registered 10x100 samples carry (ion
    # divergence 220 urad, electron 145/105 urad, read back from a
    # registered file); 1 is high acceptance.
    preset = env_value('EVGEN_AB_PRESET', '0')
    t0 = time.time()
    abconv, ab_source = afterburner_binary(args.workdir, log, preset)
    ab_build_s = round(time.time() - t0, 1)
    ab_stem = os.path.join(args.workdir, f'{stem}.ab')
    t0 = time.time()
    if run([abconv, '-p', preset, '-f', 'treeroot', '--plot-off',
            '-o', ab_stem, ascii_path], log) != 0:
        fail('afterburner failed:\n' + tail(log))
    afterburner_s = round(time.time() - t0, 1)
    produced = None
    for candidate in (f'{ab_stem}.hepmc3.tree.root', f'{ab_stem}.treeroot',
                      f'{ab_stem}.root'):
        if os.path.exists(candidate):
            produced = candidate
            break
    if produced is None:
        fail(f'abconv wrote no treeroot output for {ab_stem}:\n' + tail(log))

    events = count_tree_events(produced)
    if events is not None and events != args.events:
        fail(f'{events} events in the generated file, {args.events} requested')
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    shutil.move(produced, args.out)
    os.remove(ascii_path)
    summary = {
        'generator': generator, 'generator_version':
            env_value('EVGEN_GENERATOR_VERSION', ''),
        'process': process, 'q2_range': env_value('EVGEN_Q2_RANGE', ''),
        'state': env_value('EVGEN_STATE', ''),
        'mechanism': env_value('EVGEN_MECHANISM', ''),
        'beam_species': env_value('EVGEN_BEAM_SPECIES', 'ep'),
        'ebeam': env_value('EBEAM', ''), 'pbeam': env_value('PBEAM', ''),
        'radiative': env_value('EVGEN_RADIATIVE', 'off'),
        'afterburner_preset': preset, 'afterburner': ab_source,
        'afterburner_build_seconds': ab_build_s, 'seed': seed,
        'events_requested': args.events, 'events': events,
        'card': card_path, 'output': args.out,
        'output_bytes': os.path.getsize(args.out),
        'generation_seconds': generation_s,
        'afterburner_seconds': afterburner_s,
        'total_seconds': round(time.time() - started, 1),
    }
    with open(summary_path, 'w') as handle:
        json.dump(summary, handle, indent=2)
    print(f'evgen: {events if events is not None else args.events} events, '
          f'seed {seed}, {summary["output_bytes"]} bytes at {args.out} '
          f'({generation_s} s generation, {afterburner_s} s afterburner)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
