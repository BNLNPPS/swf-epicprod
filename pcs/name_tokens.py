"""
Helpers for PCS composed names and their physical suffixes.

PCS uses one logical composed name for the campaign task/dataset identity:

    group.EIC.26.06.0.epic_craterlake.p3001.e1.s1.r1[.kN][.sample]

Physical systems append transport/attempt suffixes to that logical identity.
Those suffixes are not part of the logical PCS name:

    logical.b1          Rucio block 1
    logical.try2        second PanDA/JEDI submission attempt
    logical.try2.b1     block 1 of the second attempt

Keep these rules here rather than spreading positional regexes through model,
service, and monitor code. New dynamic suffixes should be added to
TERMINAL_SUFFIX_PATTERNS and documented in docs/PCS.md.
"""
from dataclasses import dataclass
import re


BACKGROUND_TAG_RE = re.compile(r'^k(?P<number>\d+)$')

TERMINAL_SUFFIX_PATTERNS = {
    'try': re.compile(r'^try(?P<number>[1-9]\d*)$'),
    'block': re.compile(r'^b(?P<number>[1-9]\d*)$'),
}


@dataclass(frozen=True)
class NameSuffix:
    """One parsed terminal suffix, in left-to-right physical-name order."""

    kind: str
    token: str
    number: int


def match_terminal_suffix(token, suffix_kinds=None):
    """Return a NameSuffix for one reserved terminal token, or None."""
    allowed = set(suffix_kinds or TERMINAL_SUFFIX_PATTERNS)
    for kind, pattern in TERMINAL_SUFFIX_PATTERNS.items():
        if kind not in allowed:
            continue
        match = pattern.fullmatch(token or '')
        if match:
            return NameSuffix(kind=kind, token=token, number=int(match.group('number')))
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


def sample_name_reserved_collision(sample_name):
    """Return True when a sample name would collide with reserved PCS tokens."""
    if not sample_name:
        return False
    segments = str(sample_name).split('.')
    return bool(
        BACKGROUND_TAG_RE.fullmatch(segments[0])
        or match_terminal_suffix(segments[-1])
    )


def reserved_sample_token_description():
    return 'first segment k<n> or last segment b<n>/try<n>'
