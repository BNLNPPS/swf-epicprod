#!/usr/bin/env python3
"""compare-pythia8.py: the internal EVGEN pythia8 stage against a registered sample.

Generates N neutral-current DIS events with the payload's own composer and
driver (payload/evgen_generate.py, before the afterburner) and reads the
first N events of a registered sample (a hepmc3.tree.root file, local or
root://), then prints the quantities docs/EPICPROD_INTERNAL_EVGEN.md
§ Comparison with the registered sample tabulates: the hard process, Q
mean and median, x mean, the cross section, final-state multiplicity and
photons per event. Lorentz invariants only, so the registered sample's
crossing angle and beam spreads do not enter.

Runs inside the campaign image (the driver builds against its Pythia and
HepMC3; the tree file reads through its ROOT and HepMC3 rootIO)::

    apptainer exec -e -B /data /cvmfs/singularity.opensciencegrid.org/eicweb/eic_xl:26.07.1-stable \\
      bash -lc 'python3 tools/evgen/compare-pythia8.py --events 3000 --ebeam 10 --pbeam 100 \\
                --q2 q2_10to100 --registered root://dtn-eic.jlab.org//volatile/eic/EPIC/EVGEN/DIS/pythia8.316-1.0/NC/noRad/ep/10x100/q2_10to100/pythia8.316-1.0_NC_noRad_ep_10x100_q2_10to100_run000.hepmc3.tree.root \\
                --workdir /data/swf-tmp/evgen-compare'
"""
import argparse
import os
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PAYLOAD = os.path.join(HERE, '..', '..', 'swf_epicprod', 'payload')
sys.path.insert(0, PAYLOAD)


def generate(args, workdir):
    os.environ.update({'EBEAM': str(args.ebeam), 'PBEAM': str(args.pbeam),
                       'EVGEN_Q2_RANGE': args.q2, 'EVGEN_RADIATIVE': args.radiative,
                       'EVGEN_BEAM_SPECIES': 'ep'})
    import evgen_generate as eg
    os.makedirs(workdir, exist_ok=True)
    card = os.path.join(workdir, 'pythia8.card')
    with open(card, 'w') as fh:
        fh.write(eg.pythia8_dis_nc_card(args.events, args.seed))
    ascii_path = os.path.join(workdir, 'generated.hepmc')
    log = os.path.join(workdir, 'generate.log')
    eg.generate_pythia8(card, ascii_path, workdir, log, args.seed, args.events)
    return card, ascii_path


def invariants(beams, electron):
    """Q2 and x from the beams and the scattered electron, as invariants."""
    (pe, pp) = beams
    q = [pe[i] - electron[i] for i in range(4)]
    dot = lambda a, b: a[3] * b[3] - a[0] * b[0] - a[1] * b[1] - a[2] * b[2]
    q2 = -dot(q, q)
    x = q2 / (2.0 * dot(pp, q)) if dot(pp, q) else float('nan')
    return q2, x


class Summary:
    def __init__(self):
        self.q, self.x, self.mult, self.photons, self.process = [], [], [], [], {}
        self.xsec = None

    def add(self, particles, process, xsec):
        """particles: (pid, status, (px, py, pz, e))."""
        beam_e = beam_p = None
        final, scattered = [], None
        for pid, status, mom in particles:
            if status == 4:
                if pid == 11:
                    beam_e = mom
                elif pid > 100:
                    beam_p = mom
            elif status == 1:
                final.append((pid, mom))
                if pid == 11 and (scattered is None or mom[3] > scattered[3]):
                    scattered = mom
        if beam_e is None or beam_p is None or scattered is None:
            return
        q2, x = invariants((beam_e, beam_p), scattered)
        self.q.append(q2 ** 0.5)
        self.x.append(x)
        self.mult.append(len(final))
        self.photons.append(sum(1 for pid, _ in final if pid == 22))
        self.process[process] = self.process.get(process, 0) + 1
        if xsec is not None:
            self.xsec = xsec

    def row(self):
        return {'events': len(self.q),
                'process': ', '.join(f'{k}: {v}' for k, v in sorted(self.process.items(), key=lambda kv: -kv[1])[:3]),
                'Q mean': statistics.fmean(self.q), 'Q median': statistics.median(self.q),
                'x mean': statistics.fmean(self.x),
                'xsec pb': self.xsec,
                'multiplicity': statistics.fmean(self.mult),
                'photons': statistics.fmean(self.photons)}


def read_ascii(path, n):
    from pyHepMC3 import HepMC3 as hm
    s = Summary()
    reader = hm.ReaderAscii(path)
    ev = hm.GenEvent()
    while len(s.q) < n and not reader.failed():
        if not reader.read_event(ev) or reader.failed():
            break
        parts = [(p.pid(), p.status(), (p.momentum().px(), p.momentum().py(), p.momentum().pz(), p.momentum().e()))
                 for p in ev.particles()]
        proc = ev.attribute_as_string('signal_process_id') or ''
        xs = ev.cross_section()
        s.add(parts, proc, xs.xsec() if xs else None)
    reader.close()
    return s


def read_tree(path, n):
    """The registered sample through ROOT: GenEventData rows of the
    hepmc3_tree, the cross section from the event's attribute strings."""
    import ROOT
    ROOT.gErrorIgnoreLevel = ROOT.kError
    ROOT.gSystem.Load('libHepMC3rootIO')
    tfile = ROOT.TFile.Open(path)
    if not tfile or tfile.IsZombie():
        sys.exit(f'cannot open {path}')
    tree = tfile.Get('hepmc3_tree')
    s = Summary()
    for i, entry in enumerate(tree):
        if i >= n:
            break
        ev = entry.hepmc3_event
        parts = [(p.pid, p.status, (p.momentum.px(), p.momentum.py(), p.momentum.pz(), p.momentum.e()))
                 for p in ev.particles]
        proc, xsec = '', None
        for name, value in zip(ev.attribute_name, ev.attribute_string):
            name = str(name)
            if name == 'signal_process_id':
                proc = str(value)
            elif name == 'GenCrossSection':
                xsec = float(str(value).split()[0])
        s.add(parts, proc, xsec)
    return s


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--events', type=int, default=3000)
    ap.add_argument('--ebeam', type=int, required=True)
    ap.add_argument('--pbeam', type=int, required=True)
    ap.add_argument('--q2', required=True, help='physics-tag q2 range, e.g. q2_10to100')
    ap.add_argument('--radiative', default='off')
    ap.add_argument('--seed', type=int, default=1)
    ap.add_argument('--registered', required=True, help='hepmc3.tree.root path or root:// URL')
    ap.add_argument('--workdir', required=True)
    ap.add_argument('--generated', help='reuse an already generated ASCII file instead of generating')
    args = ap.parse_args()
    if args.generated:
        card, ascii_path = None, args.generated
    else:
        card, ascii_path = generate(args, args.workdir)
        print(f'card: {card}\ngenerated: {ascii_path}')
    stage = read_ascii(ascii_path, args.events).row()
    reg = read_tree(args.registered, args.events).row()
    print(f"{'quantity':<14}{'stage':>18}{'registered':>18}")
    for key in stage:
        a, b = stage[key], reg[key]
        fmt = (lambda v: f'{v:.4g}' if isinstance(v, float) else str(v))
        print(f'{key:<14}{fmt(a):>18}{fmt(b):>18}')


if __name__ == '__main__':
    main()
