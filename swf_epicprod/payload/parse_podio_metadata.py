#!/usr/bin/env python3
"""Extract geometry_config and gun parameters from podio runs frame, print as JSON.

Usage:
  parse_podio_metadata.py <edm4hep.root> [--gun] [--no-beam]

  --gun      Also extract gun parameters (for single particle runs)
  --no-beam  Omit beam energy and ion species fields (for background runs)
"""

import sys
import math
import json
import re
import podio.root_io

from shared_utils import detect_generator, detect_q2, detect_pwg, detect_dsc


def rad_to_deg(r):
    return round(float(r) * 180 / math.pi, 2)


def get_str(params, key):
    """Return string value for key, or None if not set or stored as 'None'."""
    v = params.get['std::string'](key)
    if not v.has_value():
        return None
    s = v.value()
    return None if s == "None" else s


include_gun = "--gun" in sys.argv
no_beam = "--no-beam" in sys.argv
rootfile = next(a for a in sys.argv[1:] if not a.startswith("--"))

reader = podio.root_io.Reader(rootfile)
frame = next(iter(reader.get("runs")))
params = frame.get_parameters()

result = {}

# generator and q2 from inputFiles path
input_files = get_str(params, "inputFiles")
if input_files:
    input_path = input_files.strip("[]'\" ")
    result["generator"] = detect_generator(input_path, is_single=include_gun)
    if not include_gun and not no_beam:
        result["requester_pwg"] = detect_pwg(input_path)
    q2_min, q2_max = detect_q2(input_path)
    if q2_min is not None:
        result["q2_min_gev2"] = q2_min
    if q2_max is not None:
        result["q2_max_gev2"] = q2_max

# is_background_mixed from hepmc_merger_background_files
bg_files = get_str(params, "hepmc_merger_background_files")
result["is_background_mixed"] = bool(bg_files and bg_files.strip() not in ("", "None", "[]"))

# requester_dsc from path keywords or is_background_mixed
if input_files:
    dsc = detect_dsc(input_path, is_background_mixed=result["is_background_mixed"])
    if dsc:
        result["requester_dsc"] = dsc

# data_level from outputFile extension: edm4hep -> simulation, edm4eic -> reconstruction
output_file = get_str(params, "outputFile")
if output_file:
    output_file = output_file.strip("[]'\" ")
    if ".edm4hep." in output_file:
        result["data_level"] = "simulation"
    elif ".edm4eic." in output_file:
        result["data_level"] = "reconstruction"

# geometry_config from compactFile: strip path and .xml, strip leading "epic_"
# compactFile is stored as a list string like "['path/to/file.xml']"
compact_file = get_str(params, "compactFile")
if compact_file:
    compact_file = compact_file.strip("[]'\" ")
    basename = re.sub(r'\.xml$', '', compact_file.split('/')[-1])
    if basename.startswith("epic_"):
        basename = basename[len("epic_"):]
    result["geometry_config"] = basename

    # Parse electron_beam_energy, ion_beam_energy, ion_species from geometry_config
    # Format: <detector>_<ebeam>x<pbeam>[_<species>]
    # Skip for singles (--gun) and backgrounds (--no-beam)
    if not no_beam and not include_gun:
        beam_match = re.search(r'_(\d+)x(\d+)(?:_(.+))?$', basename)
        if beam_match:
            result["electron_beam_energy_gev"] = int(beam_match.group(1))
            result["ion_beam_energy_gev"] = int(beam_match.group(2))
            species = beam_match.group(3)
            result["ion_species"] = species if species is not None else "p"

if include_gun:
    # gun_particle
    particle = get_str(params, "gun.particle")
    if particle:
        result["gun_particle"] = particle

    # gun_distribution
    distribution = get_str(params, "gun.distribution")
    if distribution:
        result["gun_distribution"] = distribution

    # gun_momentum: prefer gun.energy (MeV -> GeV)
    # gun.momentumMin/Max default to 0/10000 in npsim when not explicitly set
    energy = get_str(params, "gun.energy")
    mom_min = get_str(params, "gun.momentumMin")
    mom_max = get_str(params, "gun.momentumMax")
    npsim_defaults = (mom_min == "0.0" and mom_max == "10000.0")

    if energy is not None:
        momentum_gev = round(float(energy) / 1000, 6)
        result["gun_momentum_min_gev"] = momentum_gev
        result["gun_momentum_max_gev"] = momentum_gev
    elif not npsim_defaults and mom_min is not None and mom_max is not None:
        result["gun_momentum_min_gev"] = round(float(mom_min) / 1000, 6)
        result["gun_momentum_max_gev"] = round(float(mom_max) / 1000, 6)

    # gun_theta: radians -> degrees
    theta_min = get_str(params, "gun.thetaMin")
    theta_max = get_str(params, "gun.thetaMax")
    if theta_min is not None:
        result["gun_theta_min_deg"] = rad_to_deg(theta_min)
    if theta_max is not None:
        result["gun_theta_max_deg"] = rad_to_deg(theta_max)

    # gun_phi: radians -> degrees; default 0 and 360
    phi_min = get_str(params, "gun.phiMin")
    phi_max = get_str(params, "gun.phiMax")
    result["gun_phi_min_deg"] = rad_to_deg(phi_min) if phi_min is not None else 0
    result["gun_phi_max_deg"] = rad_to_deg(phi_max) if phi_max is not None else 360

print(json.dumps(result))
