#!/usr/bin/env python3
"""evgen_djangoh_hepmc3.py — DJANGOH's event file to HepMC3 ASCII
(docs/EPICPROD_INTERNAL_EVGEN.md § The steering, DJANGOH).

DJANGOH, as patched for the payload (djangoh-epicprod.patch), writes the
JETSET event record of every hadronized event to <name>_evt.dat in the
layout eic-smear reads: a header row per event (event number, channel,
LEPTO process codes, kinematics, the sampled cross section, the track
count), a rule, one row per record entry (I, K(I,1..5), P(I,1..5),
V(I,1..3), the hadron beam along +z), and an "Event finished" rule. This
converter keeps what the afterburner and the simulation use: the two
beam particles (JETSET status 21, the first two entries) as HepMC status
4, and the final-state particles (JETSET status 1 to 10) as HepMC status
1, all from one vertex at the origin; the energy is recomputed from the
momentum and the mass, since the record stores single precision. The
generated cross section from the header travels as the HepMC event
attribute. Standard library only.

Usage::

    evgen_djangoh_hepmc3.py <name>_evt.dat out.hepmc [--expect N]
"""
import argparse
import math
import sys


def fail(msg):
    print(f'ERROR: {msg}', file=sys.stderr)
    sys.exit(1)


def read_events(path):
    """Yield (header fields, [track rows]) per event from the DJANGOH
    event file; header rows start with the literal 0 and carry 31
    fields, track rows carry 14."""
    header = None
    tracks = []
    in_tracks = False
    with open(path) as handle:
        for line in handle:
            parts = line.split()
            if not parts:
                continue
            if 'Event finished' in line:
                if header is not None:
                    yield header, tracks
                header, tracks, in_tracks = None, [], False
                continue
            if parts[0].startswith('='):
                if header is not None:
                    in_tracks = True
                continue
            if header is None and parts[0] == '0' and len(parts) == 31:
                header = parts
                continue
            if in_tracks and len(parts) == 14:
                tracks.append(parts)


def convert(src, dst, expect=None):
    n_written = 0
    with open(dst, 'w') as out:
        out.write('HepMC::Version 3.03.00\n')
        out.write('HepMC::Asciiv3-START_EVENT_LISTING\n')
        for header, tracks in read_events(src):
            event = int(header[1])
            sigma_nb = float(header[18])   # SIGtot, in nanobarn (HERACLES)
            beams, final = [], []
            for row in tracks:
                index, ks, kf = int(row[0]), int(row[1]), int(row[2])
                px, py, pz, _e, m = (float(v) for v in row[6:11])
                energy = math.sqrt(px * px + py * py + pz * pz + m * m)
                if ks == 21 and index <= 2:
                    beams.append((kf, px, py, pz, energy, m))
                elif 1 <= ks <= 10:
                    final.append((kf, px, py, pz, energy, m))
            if len(beams) != 2:
                fail(f'event {event}: {len(beams)} beam entries, 2 expected')
            if not final:
                fail(f'event {event}: no final-state particles')
            n_written += 1
            out.write(f'E {n_written} 1 {len(beams) + len(final)}\n')
            out.write('U GEV MM\n')
            out.write(f'A 0 GenCrossSection {sigma_nb * 1e3:.6e} 0 -1 -1\n')
            for i, (kf, px, py, pz, e, m) in enumerate(beams, 1):
                out.write(f'P {i} 0 {kf} {px:.9e} {py:.9e} {pz:.9e} {e:.9e} {m:.9e} 4\n')
            out.write('V -1 0 [1,2]\n')
            for i, (kf, px, py, pz, e, m) in enumerate(final, len(beams) + 1):
                out.write(f'P {i} -1 {kf} {px:.9e} {py:.9e} {pz:.9e} {e:.9e} {m:.9e} 1\n')
        out.write('HepMC::Asciiv3-END_EVENT_LISTING\n')
    if expect is not None and n_written != expect:
        fail(f'{n_written} events converted, {expect} expected')
    return n_written


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('src', help='DJANGOH <name>_evt.dat')
    ap.add_argument('dst', help='HepMC3 ASCII output')
    ap.add_argument('--expect', type=int, default=None,
                    help='the event count the file must hold')
    args = ap.parse_args()
    n = convert(args.src, args.dst, args.expect)
    print(f'{n} events written to {args.dst}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
