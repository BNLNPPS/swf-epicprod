"""Shared utilities for EIC simulation campaign metadata scripts."""

import re

pwg_override_map = {
    "D0_ABCONV": "jets_hf",
    "Lc_ABCONV": "jets_hf",
    "DIJET_ABCONV": "jets_hf",
    "DIS": "inclusive",
    "SIDIS": "semi_inclusive",
    "EXCLUSIVE": "edt",
}


def detect_pwg(path):
    """Detect requester_pwg from a file path or DID path.

    Returns:
        PWG string matching the schema enum, defaulting to 'other'.
    """
    for key, value in pwg_override_map.items():
        if f"/{key}/" in path or path.endswith(f"/{key}"):
            return value
    return "other"


def detect_dsc(path, is_background_mixed=False):
    """Detect requester_dsc from a file path or DID path.

    Args:
        path: File path or Rucio DID path string.
        is_background_mixed: True if the dataset includes background mixing.

    Returns:
        DSC string if detected, or None.
    """
    if is_background_mixed or "Backgrounds" in path or "Bkg" in path or "BACKGROUNDS" in path:
        return "tracking"
    return None


generators_list_ci = [
    "pythia6", "pythia8", "beagle", "djangoh", "rapgap", "dempgen",
    "sartre", "lager", "estarlight", "getalm", "eicmesonsfgen",
    "eic_sr_geant4", "eic_esr_xsuite", "sherpa",
]

generator_override_map = {
    "SYNRAD": "eic_sr_geant4",
    "DIS/NC": "pythia8",
    "DIS/CC": "pythia8",
    "UPSILON_ABCONV": "estarlight",
}

_gen_pattern_ci = r'(' + '|'.join(generators_list_ci) + r')'
_gen_pattern_epic = r'(EpIC)'


def detect_q2(path):
    """Extract q2_min and q2_max from a file path or DID path.

    Returns:
        Tuple (q2_min, q2_max) where either may be None if not found.
    """
    q2_min, q2_max = None, None
    range_match = re.search(r'q2_(\d+)(?:to|_)(\d+)', path, re.IGNORECASE)
    min_only_match = re.search(r'minQ2=(\d+)', path)
    q2_single_match = re.search(r'(?<![a-zA-Z])q2_(\d+)(?![to\d_])', path, re.IGNORECASE)

    if range_match:
        q2_min, q2_max = int(range_match.group(1)), int(range_match.group(2))
    elif min_only_match:
        q2_min = int(min_only_match.group(1))
    elif q2_single_match:
        q2_min = int(q2_single_match.group(1))

    return q2_min, q2_max


def detect_generator(path, is_single=False):
    """Detect generator name from a file path or DID path.

    Args:
        path: File path or Rucio DID path string.
        is_single: True if this is a single-particle run.

    Returns:
        Generator name string matching the schema enum.
    """
    if is_single:
        return "single_particle"
    generator = "other"
    if re.search(_gen_pattern_epic, path):
        generator = "epic"
    else:
        m = re.search(_gen_pattern_ci, path, re.IGNORECASE)
        if m:
            generator = m.group(1).lower()
    for key, value in generator_override_map.items():
        if f"/{key}/" in path or path.endswith(f"/{key}"):
            generator = value
            break
    return generator
