"""
Helpers for PCS composed names and their physical suffixes.

PCS uses one logical composed name for the campaign task/dataset identity:

    group.EIC.26.06.0.epic_craterlake.p3001.e1.s1.r1[.kN][.sample]

Physical systems append transport/attempt suffixes to that logical identity.
Those suffixes are not part of the logical PCS name:

    logical.b1          Rucio block 1
    logical.try2        second PanDA/JEDI submission attempt
    logical.try2.b1     block 1 of the second attempt

One suffix belongs to the logical identity rather than to transport, and is
therefore never stripped by default (docs/PCS.md, Trials):

    logical.trial       the first trial of this configuration
    logical.trial2      the second, and so on

Keep these rules here rather than spreading positional regexes through model,
service, and monitor code. New dynamic suffixes should be added to
TERMINAL_SUFFIX_PATTERNS (transport) or LOGICAL_SUFFIX_PATTERNS (identity)
and documented in docs/PCS.md.
"""
from dataclasses import dataclass
import re


BACKGROUND_TAG_RE = re.compile(r'^k(?P<number>\d+)$')

TERMINAL_SUFFIX_PATTERNS = {
    'try': re.compile(r'^try(?P<number>[1-9]\d*)$'),
    'block': re.compile(r'^b(?P<number>[1-9]\d*)$'),
}

# A logical suffix is part of the PCS identity rather than transport. A
# trial is a standing task of its own, modelled on the configuration it
# proves and carrying its own event count and outputs (docs/PCS.md,
# Trials), so stripping its suffix would collapse it onto that
# configuration. Logical suffixes are therefore kept out of the default
# stripping set and matched only when a caller names the kind. Bare
# ``trial`` is the first trial, as a bare logical name is attempt 1;
# further trials number from 2, for the variants a configuration needs
# before it is right.
LOGICAL_SUFFIX_PATTERNS = {
    'trial': re.compile(r'^trial(?P<number>[2-9]\d*)?$'),
}

ALL_SUFFIX_PATTERNS = {**TERMINAL_SUFFIX_PATTERNS, **LOGICAL_SUFFIX_PATTERNS}


@dataclass(frozen=True)
class NameSuffix:
    """One parsed terminal suffix, in left-to-right physical-name order."""

    kind: str
    token: str
    number: int


def match_terminal_suffix(token, suffix_kinds=None):
    """Return a NameSuffix for one reserved terminal token, or None."""
    allowed = set(suffix_kinds or TERMINAL_SUFFIX_PATTERNS)
    for kind, pattern in ALL_SUFFIX_PATTERNS.items():
        if kind not in allowed:
            continue
        match = pattern.fullmatch(token or '')
        if match:
            # A suffix whose number is optional carries 1 when bare.
            number = int(match.group('number') or 1)
            return NameSuffix(kind=kind, token=token, number=number)
    return None


def split_terminal_suffixes(name, suffix_kinds=None):
    """Split reserved terminal suffixes from a composed or physical name.

    Suffixes are recognized right-to-left so both ``logical.b1`` and
    ``logical.try2.b1`` produce the same logical base. The returned suffix tuple
    is restored to left-to-right order: ``(try2, b1)`` for
    ``logical.try2.b1``.
    """
    value = (name or '').strip()
    if not value:
        return '', ()

    parts = value.split('.')
    found = []
    while len(parts) > 1:
        suffix = match_terminal_suffix(parts[-1], suffix_kinds=suffix_kinds)
        if suffix is None:
            break
        found.append(suffix)
        parts.pop()

    return '.'.join(parts), tuple(reversed(found))


def suffix_number(suffixes, kind):
    """Return the rightmost parsed suffix number for ``kind``, if present."""
    for suffix in reversed(tuple(suffixes or ())):
        if suffix.kind == kind:
            return suffix.number
    return None


def logical_name_from_physical_name(name):
    """Return the logical PCS name by stripping all known terminal suffixes."""
    base, _suffixes = split_terminal_suffixes(name)
    return base


def panda_name_from_physical_name(name):
    """Return the PanDA task/outDS name by stripping only Rucio block suffixes."""
    base, _suffixes = split_terminal_suffixes(name, suffix_kinds=('block',))
    return base


def panda_attempt_name(logical_name, try_number):
    """Canonical physical PanDA task/output name for a submission attempt."""
    attempt = int(try_number)
    if attempt < 1:
        raise ValueError('try_number must be >= 1')
    return logical_name if attempt == 1 else f'{logical_name}.try{attempt}'


def try_number_from_physical_name(logical_name, physical_name):
    """Resolve a physical task/output name back to its PanDA attempt number.

    ``logical`` and ``logical.b1`` are attempt 1. ``logical.try2`` and
    ``logical.try2.b1`` are attempt 2. A non-matching base returns None.
    """
    base, suffixes = split_terminal_suffixes(physical_name, suffix_kinds=('try', 'block'))
    if base != logical_name:
        return None
    return suffix_number(suffixes, 'try') or 1


def trial_name(logical_name, trial_number=1):
    """The logical name of a trial of ``logical_name``: ``.trial`` for
    the first, ``.trial<n>`` after that (docs/PCS.md, Trials)."""
    number = int(trial_number or 0)
    if number < 1:
        raise ValueError('trial_number must be >= 1')
    token = 'trial' if number == 1 else f'trial{number}'
    return f'{logical_name}.{token}'


def trial_number_from_name(name):
    """The trial number a name carries, or 0 when it names no trial.
    Physical suffixes are stripped first, so a trial's physical forms
    answer the same as its logical name."""
    base, _physical = split_terminal_suffixes(name, suffix_kinds=('try', 'block'))
    _logical, suffixes = split_terminal_suffixes(base, suffix_kinds=('trial',))
    return suffix_number(suffixes, 'trial') or 0


def trial_subject_name(name):
    """The configuration a trial proves: the name with its trial suffix
    removed. A name that is no trial is returned unchanged."""
    base, _physical = split_terminal_suffixes(name, suffix_kinds=('try', 'block'))
    logical, _suffixes = split_terminal_suffixes(base, suffix_kinds=('trial',))
    return logical


def sample_name_reserved_collision(sample_name):
    """Return True when a sample name would collide with reserved PCS
    tokens. The trial suffix is among them: no physics variant may be
    named for one, which is what keeps the parse unambiguous."""
    if not sample_name:
        return False
    segments = str(sample_name).split('.')
    return bool(
        BACKGROUND_TAG_RE.fullmatch(segments[0])
        or match_terminal_suffix(segments[-1],
                                 suffix_kinds=tuple(ALL_SUFFIX_PATTERNS))
    )


def reserved_sample_token_description():
    return 'first segment k<n>, or last segment b<n>/try<n>/trial[<n>]'


def campaign_family(name):
    """The campaign family of a version name: its first two fields.

    The campaign is the family (26.07); the third field is the patch
    level of the monthly software release, recorded on datasets and
    tasks but not a campaign boundary — see docs/CAMPAIGN_FAMILY.md.
    A two-field name is already the family.
    """
    parts = str(name or '').split('.')
    return '.'.join(parts[:2]) if len(parts) >= 2 else str(name or '')
