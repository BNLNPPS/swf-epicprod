"""Physics-configuration resolution (CAMPAIGN_CONTINUUM.md).

A physics configuration is the campaign-invariant identity behind
dataset editions: physics tag + evgen identity (+ background tag) +
sample variant. Editions realize it per campaign, adding that campaign's
simu/reco tags, detector config, and version segment. This module
derives a canonical configuration key from any edition and groups
editions by it — the shared core consumed by the instancing merge rule,
the request matcher, and the physics-configuration view.

Resolution policy (never guess):

- **Physics**: the bound physics tag is canonical — tags are created and
  matched on the full physics axis set, so tag equality is physics
  equality.
- **Evgen**: the path observation is derived with
  ``physics_match.derive_evgen`` and compared with the bound tag. Where
  they agree (or only one side resolves), that identity is used. Where
  they conflict, the path wins and the edition is flagged — the known
  overloaded-default bindings (e.g. ``e1`` carrying pythia8 8.310 on
  editions whose paths name other generators) make tag-trust unsafe,
  while a curated tag on a path that derives nothing stands unchallenged.
  Where neither side resolves, the configuration is **unresolved** and
  keyed by its own tag labels so it can never falsely merge.
- **Sample**: the stored ``sample_name``, else the single-particle angle
  derived from the path; empty means the configuration has no variant.
"""
from .physics_match import _KNOWN_AREAS, derive_evgen, single_particle_angle


def _source_path(dataset):
    """The edition's observed path: the recorded source location with any
    Rucio scope prefix and leading stage/version segments stripped down to
    the physics-bearing tail."""
    location = ((dataset.metadata or {}).get('source') or {}).get('location', '')
    if not location:
        return ''
    path = location.split(':', 1)[-1].lstrip('/')
    if '/' not in path and path.startswith('group.'):
        # A PanDA-convention dot-named DID (the naming ruling, PCS.md):
        # the readable tail after scope/version/detector follows the same
        # physics grammar dot-separated. Dot-splitting fragments dotted
        # version tokens (pythia8.316-1.0 -> pythia8, 316-1, 0); a pure
        # version fragment re-merges into its predecessor, restoring the
        # slash-path segment shape. Non-version tokens (100MeV, 18x275,
        # q2_1to10) never match the fragment pattern.
        import re
        parts = path.split('.')
        for i, token in enumerate(parts):
            if token in _KNOWN_AREAS:
                segments = []
                for part in parts[i:]:
                    if segments and re.fullmatch(r'\d+(?:-\d+)*', part):
                        segments[-1] += '.' + part
                    else:
                        segments.append(part)
                path = '/'.join(['EVGEN'] + segments)
                break
        else:
            return ''
    segs = path.split('/')
    # physics_match expects EVGEN-rooted paths. A produced
    # RECO/FULL/SIMU path (<stage>/<version>/<detector>/<physics...>)
    # carries the same physics grammar after its three head segments, so
    # it reduces onto the EVGEN root; an EVGEN path keeps its root.
    for head in ('RECO', 'FULL', 'SIMU'):
        if head in segs[:6]:
            idx = segs.index(head)
            return 'EVGEN/' + '/'.join(segs[idx + 3:])
    if 'EVGEN' in segs[:6]:
        return '/'.join(segs[segs.index('EVGEN'):])
    return path


def evgen_identity(dataset):
    """(identity, provenance) for the edition's evgen axis.

    identity: (generator, generator_version, radiative) lowercased, or
    None when unresolved. provenance: 'tag' | 'path' | 'tag+path' |
    'path-over-tag' (conflict, path won) | 'unresolved'.
    """
    tag = dataset.evgen_tag
    params = (tag.parameters or {}) if tag else {}
    tag_key = None
    if params.get('generator'):
        tag_key = (str(params.get('generator', '')).lower(),
                   str(params.get('generator_version', '')).lower(),
                   str(params.get('radiative', '')).lower())
    derived = derive_evgen(_source_path(dataset))
    path_key = None
    if derived and derived.get('generator'):
        path_key = (str(derived.get('generator', '')).lower(),
                    str(derived.get('generator_version', '')).lower(),
                    str(derived.get('radiative', '')).lower())

    if tag_key and path_key:
        if tag_key == path_key:
            return tag_key, 'tag+path'
        return path_key, 'path-over-tag'
    if tag_key:
        return tag_key, 'tag'
    if path_key:
        return path_key, 'path'
    return None, 'unresolved'


def sample_identity(dataset):
    """The edition's sample variant: stored name, else the derived
    single-particle angle, else ''."""
    if dataset.sample_name:
        return dataset.sample_name
    return single_particle_angle(_source_path(dataset)) or ''


def physics_config_key(dataset):
    """The edition's physics-configuration key and resolution detail.

    Returns {'key': tuple, 'evgen': tuple|None, 'evgen_source': str,
    'sample': str, 'resolved': bool}. The key is hashable and stable:
    (physics tag label, evgen identity, background tag label, sample).
    Unresolved evgen keys embed the edition's own tag labels so distinct
    unresolved editions never merge.
    """
    physics = dataset.physics_tag.tag_label if dataset.physics_tag else ''
    background = dataset.background_tag.tag_label if dataset.background_tag else ''
    evgen, source = evgen_identity(dataset)
    sample = sample_identity(dataset)
    if evgen is None:
        evgen_part = ('unresolved',
                      dataset.evgen_tag.tag_label if dataset.evgen_tag else '',
                      dataset.pk)
    else:
        evgen_part = evgen
    return {
        'key': (physics, evgen_part, background, sample),
        'evgen': evgen,
        'evgen_source': source,
        'sample': sample,
        'resolved': evgen is not None and bool(physics),
    }


def group_editions(datasets):
    """Group dataset editions by physics configuration.

    Returns {key: {'editions': [(dataset, detail), ...],
    'campaigns': {campaign_name, ...}}} — one entry per configuration,
    its editions across whatever campaigns the input spans. Callers pass
    one row per composed identity (block rows collapse upstream).

    Two passes: the first resolves every edition and observes which evgen
    tags conflict with path derivations anywhere in the population; the
    second demotes tag-only resolutions on those observed-overloaded tags
    to unresolved — a tag caught asserting the wrong generator elsewhere
    (the e1 default-binding pool) cannot stand uncorroborated, and an
    unresolved edition keys uniquely so it can never falsely merge.
    """
    details = [(dataset, physics_config_key(dataset)) for dataset in datasets]
    overloaded = {
        dataset.evgen_tag_id for dataset, detail in details
        if detail['evgen_source'] == 'path-over-tag' and dataset.evgen_tag_id
    }
    groups = {}
    for dataset, detail in details:
        if (detail['evgen_source'] == 'tag'
                and dataset.evgen_tag_id in overloaded):
            detail = dict(detail)
            detail['evgen'] = None
            detail['evgen_source'] = 'unresolved'
            detail['resolved'] = False
            physics, _, background, sample = detail['key']
            detail['key'] = (physics,
                             ('unresolved',
                              dataset.evgen_tag.tag_label, dataset.pk),
                             background, sample)
        entry = groups.setdefault(detail['key'],
                                  {'editions': [], 'campaigns': set()})
        entry['editions'].append((dataset, detail))
        if dataset.campaign_id:
            entry['campaigns'].add(dataset.campaign.name)
    return groups
