"""PC ingest — physics configurations from legacy submission lines.

The production team's pre-PCS submissions are env-prefixed calls of
``submit_csv.sh`` (eic/job_submission_condor): the environment carries
the campaign settings (detector version and config, container tag,
PanDA resources, copy flags, RSEs) and the positional arguments name
the dataset definition by its CSV path in eic/simulation_campaign_datasets.
This module turns any number of such lines into physics configurations:
each line is parsed, its physics identity derived by the same token
scanners and tag matchers the catalog import uses, classified against
the physics configurations the catalog holds, and, on acceptance,
composed as an edition for the campaign the line names — which mints the
configuration, the only way one comes into being (PCS.md § Datasets).

Row states, none of them swallowed:

- ``identified`` — a physics configuration with this identity exists.
- ``new`` — no configuration has this identity; accept composes it.
- ``near_miss`` — the physics tag exists with configurations that differ
  only in generator version, radiation, background, or sample; shown
  with the differing axis, accepted only individually.
- ``unresolved`` — the physics is derived but the generator is not;
  left for manual association, never guessed.
- ``unparsed`` — the line is not a submission call or names no
  recognizable dataset definition; the reason is stated.

See docs/PCS_INGEST.md.
"""
import csv as _csv
import json as _json
import os as _os
import re as _re
import shlex as _shlex

from django.db import transaction

from .physics_match import (
    derive_physics, derive_background, derive_evgen, single_particle_angle,
)
from .physics_config import config_name, evgen_display as _evgen_display


_ENV_RE = _re.compile(r'^([A-Z][A-Z0-9_]*)=(.*)$', _re.S)
_BEAM_RE = _re.compile(r'(?<![\dx])(\d+x\d+)(?![\dx])')
_SUBMIT_SCRIPT = 'submit_csv.sh'

#: Campaign-side settings named by a line, shown on the row as "as
#: previously submitted"; not acted on by acceptance.
_CAMPAIGN_ENV = ('DETECTOR_VERSION', 'DETECTOR_CONFIG', 'JUG_XL_TAG')
_JOB_ENV = ('PANDATE', 'PANDA_SITE', 'PANDA_QUEUE', 'PANDA_MEMORY',
            'PANDA_DISK', 'PANDA_WALLTIME', 'PANDA_MAX_ATTEMPT',
            'PANDA_SKIP_SCOUT', 'PANDA_NCORE', 'OUT_RSE', 'LOG_RSE',
            'COPYRECO', 'COPYFULL', 'COPYLOG', 'USERUCIO', 'BG_FILES',
            'SIGNAL_FREQ', 'SIGNAL_STATUS', 'TAG_PREFIX', 'TAG_SUFFIX')

#: Physics axes that define a family; everything else the physics tag
#: carries (Q² range above all) discriminates within it.
_FAMILY_AXES = ('process', 'beam_energy_electron', 'beam_energy_hadron',
                'beam_species', 'particle', 'gun_energy')


# ── parsing ──────────────────────────────────────────────────────────────────

def parse_line(raw):
    """One legacy line → its parts. Never raises: a line that is not a
    submission call comes back with ``error`` set and the rest empty."""
    out = {'raw': raw, 'env': {}, 'script': '', 'template': '', 'type': '',
           'csv_path': '', 'target_hours': '', 'extra': [], 'error': ''}
    text = (raw or '').strip()
    if not text or text.startswith('#'):
        out['error'] = 'empty line'
        return out
    try:
        tokens = _shlex.split(text)
    except ValueError as e:
        out['error'] = f'cannot tokenize: {e}'
        return out
    positional = []
    for tok in tokens:
        m = _ENV_RE.match(tok)
        if m and not positional:
            out['env'][m.group(1)] = m.group(2)
        else:
            positional.append(tok)
    if not positional:
        out['error'] = 'no submission call after the environment settings'
        return out
    if positional[0].endswith(_SUBMIT_SCRIPT):
        out['script'] = positional[0]
        args = positional[1:]
        if len(args) < 3:
            out['error'] = (f'{_SUBMIT_SCRIPT} takes template, type, and the '
                            f'CSV path; got {len(args)} argument(s)')
            return out
        out['template'], out['type'], out['csv_path'] = args[0], args[1], args[2]
        if len(args) > 3:
            out['target_hours'] = args[3]
        out['extra'] = args[4:]
    elif positional[0].lower().endswith('.csv') and len(positional) == 1:
        # A bare definition path is accepted as a line naming only the
        # dataset; the campaign settings are then absent.
        out['csv_path'] = positional[0]
    else:
        out['error'] = (f'not a {_SUBMIT_SCRIPT} call: the first argument '
                        f'after the environment is {positional[0]!r}')
        return out
    out['csv_path'] = out['csv_path'].strip().lstrip('/')
    if not out['csv_path'].lower().endswith('.csv'):
        out['error'] = f'the dataset argument {out["csv_path"]!r} is not a CSV path'
    return out


# ── definitions ──────────────────────────────────────────────────────────────

def _definitions_by_path():
    """The dataset definitions inventory from the nightly snapshot, keyed by
    CSV path (case-insensitive), with the snapshot stamp. Empty when no
    snapshot is recorded — the caller states that on every row."""
    from .services import RUCIO_SNAPSHOT_DIR
    from .definitions_sweep import SNAPSHOT_NAME
    path = _os.path.join(RUCIO_SNAPSHOT_DIR, SNAPSHOT_NAME)
    try:
        with open(path) as f:
            snap = _json.load(f)
    except (OSError, ValueError):
        return {}, None
    by_path = {}
    for entry in snap.get('definitions') or []:
        by_path[str(entry.get('path') or '').lower()] = entry
    stamp = (snap.get('summary') or {}).get('checked_at')
    return by_path, stamp


def _evgen_path_from_repo(csv_path):
    """The EVGEN directory the definition's first file lives in, read from
    the local clone of the definitions repository; '' when unavailable."""
    from .definitions_sweep import REPO_DIR
    full = _os.path.join(REPO_DIR, csv_path)
    if not _os.path.isfile(full):
        # The repository is case-preserving; a line may not be.
        parent = _os.path.dirname(full)
        try:
            for name in _os.listdir(parent):
                if name.lower() == _os.path.basename(csv_path).lower():
                    full = _os.path.join(parent, name)
                    break
            else:
                return ''
        except OSError:
            return ''
    try:
        with open(full, newline='') as f:
            first = next(_csv.reader(f), None)
    except (OSError, StopIteration):
        return ''
    if not first or not first[0].strip():
        return ''
    directory = _os.path.dirname(first[0].strip().lstrip('/'))
    if not directory:
        return ''
    return directory if directory.startswith('EVGEN/') else 'EVGEN/' + directory


def _evgen_from_catalog(evgen_path):
    """The generator identity the catalog already records for this EVGEN
    path: the evgen resolution of the catalog datasets whose source
    location is the path (the CSV import resolves the generator from the
    request's version column, which a legacy line does not carry).
    Returns (identity tuple, dataset count) when every such dataset
    agrees, (None, count) otherwise."""
    from .models import Dataset
    from .physics_config import physics_config_key
    # Recorded source locations are produced-output DIDs
    # (epic:/RECO/<version>/<config>/<physics tail>) or EVGEN request
    # paths (EVGEN/<physics tail>): the physics tail is the shared part.
    tail = evgen_path[len('EVGEN/'):] if evgen_path.startswith('EVGEN/') \
        else evgen_path
    if not tail:
        return None, 0
    found = set()
    sources = set()
    count = 0
    for ds in (Dataset.objects
               .filter(metadata__source__location__iendswith='/' + tail)
               .select_related('physics_tag', 'evgen_tag')):
        count += 1
        detail = physics_config_key(ds)
        if detail['evgen'] is not None:
            found.add(detail['evgen'])
            sources.add(detail['evgen_source'])
    if len(found) == 1:
        # A binding observed in a path is evidence; a bare tag binding
        # is only the catalog's default (the overloaded e1 case) and may
        # identify an existing configuration but never mint a new one.
        trusted = any('path' in s for s in sources)
        return next(iter(found)), count, trusted
    return None, count, False


def _evgen_path_from_name(csv_path):
    """Fallback derivation path from the CSV file name alone: the name's
    underscore tokens after the directory (generator, area, species, beam,
    Q²) laid out as path segments."""
    directory = _os.path.dirname(csv_path)
    stem = _os.path.splitext(_os.path.basename(csv_path))[0]
    tokens = [t for t in stem.split('_') if t]
    # q2 ranges are written q2_1to10 in the name: rejoin them.
    merged = []
    i = 0
    while i < len(tokens):
        if tokens[i] == 'q2' and i + 1 < len(tokens):
            merged.append('q2_' + tokens[i + 1])
            i += 2
        else:
            merged.append(tokens[i])
            i += 1
    return 'EVGEN/' + '/'.join([s for s in directory.split('/') if s] + merged)


# ── resolution ───────────────────────────────────────────────────────────────

def _beam_from(env, path):
    m = _BEAM_RE.search(path or '')
    if m:
        return m.group(1)
    e = str(env.get('EBEAM') or '').strip()
    h = str(env.get('PBEAM') or '').strip().split('_')[0]
    return f'{e}x{h}' if e and h else ''


def _physics_axes(params):
    """The family key of a derived or stored physics parameter set."""
    return tuple(str(params.get(k, '') or '') for k in _FAMILY_AXES)


def _within_family_axes(params):
    """The axes that discriminate inside a family, for display."""
    bits = []
    q2 = params.get('q2_range')
    if q2:
        bits.append(str(q2))
    for k, v in sorted(params.items()):
        if k in _FAMILY_AXES or k in ('q2_range', 'notes') or not v:
            continue
        bits.append(f'{k}={v}')
    return ' '.join(bits)


def _family(derived, generator):
    """Physics configurations sharing this line's family axes and generator
    name: (label, physics tag label, within-family axes, evgen display,
    sample) per member."""
    from .models import PhysicsConfig, PhysicsTag
    if not derived:
        return []
    want = _physics_axes(derived)
    tags = [t for t in PhysicsTag.objects.filter(
                parameters__process=derived.get('process'))
            if _physics_axes(t.parameters or {}) == want]
    if not tags:
        return []
    members = []
    gen = (generator or '').lower()
    for pc in (PhysicsConfig.objects.filter(physics_tag__in=tags)
               .select_related('physics_tag').order_by('label')):
        if gen and not pc.evgen_display.lower().startswith(gen):
            continue
        members.append({
            'label': pc.label,
            'physics_tag': pc.physics_tag.tag_label,
            'axes': _within_family_axes(pc.physics_tag.parameters or {}),
            'evgen': pc.evgen_display,
            'sample': pc.sample_name,
        })
    return members


def resolve_line(parsed, definitions=None, definitions_stamp=None):
    """Classify one parsed line against the catalog. Returns the row dict
    the page renders and the accept step re-derives; ``state`` is one of
    identified / new / near_miss / unresolved / unparsed."""
    from .models import Campaign, Dataset, PhysicsConfig
    from .name_tokens import campaign_family
    from .services import (find_or_create_physics_tag,
                           find_or_create_background_tag,
                           _no_signal_physics_tag, ServiceError)

    env = parsed['env']
    row = {
        'raw': parsed['raw'],
        'csv_path': parsed['csv_path'],
        'state': 'unparsed', 'reason': parsed['error'],
        'campaign': {k: env.get(k, '') for k in _CAMPAIGN_ENV},
        'job': {k: env[k] for k in _JOB_ENV if k in env},
        'target_hours': parsed['target_hours'],
        'extra_args': parsed['extra'],
        'definition': None, 'evgen_path': '',
        'physics': None, 'evgen': None, 'background': None, 'sample': '',
        'physics_tag': '', 'physics_tag_new': False,
        'config_key': '', 'pc': None, 'edition': None,
        'near': [], 'family': [], 'campaign_name': '',
    }
    if parsed['error']:
        return row

    if definitions is None:
        definitions, definitions_stamp = _definitions_by_path()
    definition = definitions.get(parsed['csv_path'].lower())
    if definition is not None:
        cost = definition.get('cost') or {}
        row['definition'] = {
            'path': definition.get('path'),
            'n_files': cost.get('n_files'),
            'nevents_total': cost.get('nevents_total'),
            'cost_status': definition.get('cost_status'),
            'registered': definition.get('registered'),
            'requested': definition.get('requested'),
            'stamp': definitions_stamp,
        }

    evgen_path = _evgen_path_from_repo(parsed['csv_path'])
    if not evgen_path and definition is not None and definition.get('tail'):
        evgen_path = 'EVGEN/' + str(definition['tail']).strip('/')
    if not evgen_path:
        evgen_path = _evgen_path_from_name(parsed['csv_path'])
    row['evgen_path'] = evgen_path
    if definition is None:
        row['definition_note'] = ('not in the dataset definitions inventory; '
                                  'identity derived from the path alone')

    det_version = env.get('DETECTOR_VERSION', '').strip()
    if det_version:
        row['campaign_name'] = campaign_family(det_version)
        if not Campaign.objects.filter(name=row['campaign_name']).exists():
            row['campaign_missing'] = True

    beam = _beam_from(env, evgen_path)
    derived = derive_physics(evgen_path, beam=beam)
    if derived is None:
        row['reason'] = f'no physics area recognized in {evgen_path}'
        return row
    row['physics'] = derived
    is_background = derived.get('process') in ('BEAMGAS', 'SYNRAD')

    background_label = ''
    if is_background:
        physics_tag = _no_signal_physics_tag()
        row['physics_tag'] = physics_tag.tag_label
        bg_params = derive_background(evgen_path)
        if bg_params:
            row['background'] = bg_params
            bg_tag, _ = find_or_create_background_tag(bg_params, dry_run=True)
            background_label = bg_tag.tag_label if bg_tag else '(new)'
    else:
        try:
            physics_tag, action = find_or_create_physics_tag(derived, dry_run=True)
        except ServiceError as e:
            # A derived process the tag schema does not map (no category):
            # the physics is read but cannot be tagged; left for curation.
            row['state'] = 'unresolved'
            row['reason'] = f'physics cannot be tagged: {e.detail}'
            return row
        row['physics_tag'] = physics_tag.tag_label if physics_tag else '(new)'
        row['physics_tag_new'] = physics_tag is None

    evgen = derive_evgen(evgen_path)
    if evgen is None:
        # The path names no generator version (pythia8 DIS paths, merged
        # backgrounds); the catalog may already record it for this same
        # path from the request's version column. That record is used,
        # and the row says so; disagreement or absence stays unresolved.
        identity, n_recorded, trusted = _evgen_from_catalog(evgen_path)
        if identity is not None:
            evgen = {'generator': identity[0], 'generator_version': identity[1]}
            if identity[2]:
                evgen['radiative'] = identity[2]
            row['evgen_source'] = 'catalog' if trusted else 'catalog-tag'
            row['note'] = (f'generator taken from the catalog\'s editions of '
                           f'this path ({n_recorded} dataset(s)'
                           + ('' if trusted else ', tag binding only') + ')')
        elif n_recorded:
            row['evgen_note'] = (f'{n_recorded} catalog dataset(s) record this '
                                 f'path with differing or unresolved generators')
    row['evgen'] = evgen
    row['sample'] = single_particle_angle(evgen_path)
    row['family'] = _family(derived, (evgen or {}).get('generator'))
    if evgen is None:
        row['state'] = 'unresolved'
        row['reason'] = ('generator and version not resolved from the path; '
                         'associate manually')
        if row.get('evgen_note'):
            row['reason'] += f" ({row['evgen_note']})"
        return row

    # The configuration key carries the evgen identity lowercased
    # (physics_config.evgen_identity); the display keeps the derived case.
    evgen_tuple = (str(evgen.get('generator', '')).lower(),
                   str(evgen.get('generator_version', '')).lower(),
                   str(evgen.get('radiative', '')).lower())
    detail = {'key': (row['physics_tag'] if not row['physics_tag_new'] else '',
                      evgen_tuple, background_label, row['sample']),
              'evgen': evgen_tuple}
    row['evgen_text'] = _evgen_display({'evgen': (
        evgen.get('generator', ''), evgen.get('generator_version', ''),
        evgen.get('radiative', ''))})
    untrusted = row.get('evgen_source') == 'catalog-tag'
    if row['physics_tag_new'] or background_label == '(new)':
        if untrusted:
            row['state'] = 'unresolved'
            row['reason'] = ('generator known only from the catalog\'s tag '
                             'binding on other editions of this path; not '
                             'minted from it; associate manually')
            return row
        row['state'] = 'new'
        row['reason'] = ('new physics tag' if row['physics_tag_new']
                         else 'new background tag')
        return row
    key = config_name(detail)
    row['config_key'] = key
    pc = PhysicsConfig.objects.filter(config_key=key).first()
    if pc is not None:
        row['state'] = 'identified'
        row['pc'] = pc.label
        row['reason'] = ''
        if row['campaign_name']:
            edition = (Dataset.objects.filter(physics_config=pc,
                                              campaign__name=row['campaign_name'])
                       .order_by('id').first())
            if edition is not None:
                row['edition'] = edition.composed_name
        return row
    near = []
    for other in (PhysicsConfig.objects.filter(physics_tag__tag_label=row['physics_tag'])
                  .order_by('label')):
        diffs = []
        if other.evgen_display.lower() != row['evgen_text'].lower():
            diffs.append(f'generator {other.evgen_display or "unresolved"}')
        if (other.background_tag.tag_label if other.background_tag_id else '') \
                != background_label:
            diffs.append(f'background {other.background_tag.tag_label if other.background_tag_id else "none"}')
        if other.sample_name != row['sample']:
            diffs.append(f'sample {other.sample_name or "none"}')
        near.append({'label': other.label, 'differs': ', '.join(diffs) or 'identity key'})
    row['near'] = near
    if untrusted:
        row['state'] = 'unresolved'
        row['reason'] = ('generator known only from the catalog\'s tag '
                         'binding on other editions of this path; not '
                         'minted from it; associate manually')
        if near:
            row['reason'] += ' (same physics tag as ' + ', '.join(
                n['label'] for n in near) + ')'
    elif near:
        row['state'] = 'near_miss'
        row['reason'] = 'same physics tag, differing on ' + '; '.join(
            f"{n['label']}: {n['differs']}" for n in near)
    else:
        row['state'] = 'new'
        row['reason'] = ''
    return row


def analyze(text):
    """All lines of a pasted text → rows, in order, with a state count."""
    definitions, stamp = _definitions_by_path()
    import logging
    rows = []
    for raw in (text or '').splitlines():
        if not raw.strip():
            continue
        parsed = parse_line(raw)
        try:
            rows.append(resolve_line(parsed, definitions, stamp))
        except Exception as e:                                  # noqa: BLE001
            # One line's failure is that line's row, never the whole
            # analysis; the error is logged and shown on the row.
            logging.getLogger(__name__).exception(
                'PC ingest: line analysis failed for %r', raw[:200])
            rows.append({'raw': raw, 'csv_path': parsed.get('csv_path', ''),
                         'state': 'unparsed',
                         'reason': f'analysis error: {e}',
                         'campaign': {}, 'job': {}, 'family': [], 'near': [],
                         'physics': None, 'evgen': None, 'sample': '',
                         'definition': None, 'evgen_path': '',
                         'campaign_name': '', 'pc': None, 'edition': None})
    counts = {}
    for r in rows:
        counts[r['state']] = counts.get(r['state'], 0) + 1
    return {'rows': rows, 'counts': counts, 'definitions_stamp': stamp}


# ── acceptance ───────────────────────────────────────────────────────────────

def accept_line(raw, *, created_by, allow_near_miss=False):
    """Compose the edition a line describes for the campaign it names,
    which mints the physics configuration. Re-derives the line from
    scratch; refuses anything not new (or near miss when allowed) with
    the reason. Returns the row with ``accepted`` and ``composed_name``."""
    from .models import Campaign, Dataset
    from .services import (find_or_create_physics_tag, find_or_create_evgen_tag,
                           find_or_create_background_tag, _no_signal_physics_tag,
                           _ensure_csvimport_anchors, _ensure_s0_stage_tag,
                           _ensure_r0_stage_tag, ServiceError)

    row = resolve_line(parse_line(raw))
    row['accepted'] = False
    if row['state'] == 'identified':
        row['refusal'] = f"already defined as {row['pc']}"
        return row
    if row['state'] == 'near_miss' and not allow_near_miss:
        row['refusal'] = 'near miss; accept individually after review'
        return row
    if row['state'] not in ('new', 'near_miss'):
        row['refusal'] = row['reason'] or row['state']
        return row
    env = parse_line(raw)['env']
    det_version = env.get('DETECTOR_VERSION', '').strip()
    det_config = env.get('DETECTOR_CONFIG', '').strip()
    if not det_version or not det_config:
        row['refusal'] = ('the line names no DETECTOR_VERSION and '
                          'DETECTOR_CONFIG; an edition needs its campaign')
        return row
    campaign = Campaign.objects.filter(name=row['campaign_name']).first()
    if campaign is None:
        row['refusal'] = (f'campaign {row["campaign_name"]} is not defined; '
                          f'create it first')
        return row

    derived = row['physics']
    with transaction.atomic():
        background_tag = None
        if derived.get('process') in ('BEAMGAS', 'SYNRAD'):
            physics_tag = _no_signal_physics_tag()
            if row['background']:
                background_tag, _ = find_or_create_background_tag(
                    row['background'], created_by=created_by)
        else:
            physics_tag, _ = find_or_create_physics_tag(
                derived, created_by=created_by)
        evgen_tag, _ = find_or_create_evgen_tag(row['evgen'], created_by=created_by)
        s0 = _ensure_s0_stage_tag(created_by=created_by)
        r0 = _ensure_r0_stage_tag(created_by=created_by)
        probe = Dataset(scope='group.EIC', detector_version=det_version,
                        detector_config=det_config, physics_tag=physics_tag,
                        evgen_tag=evgen_tag, simu_tag=s0, reco_tag=r0,
                        background_tag=background_tag, sample_name=row['sample'])
        composed = probe.build_dataset_name()
        if Dataset.objects.filter(composed_name=composed).exists():
            row['refusal'] = f'edition {composed} already exists'
            return row
        ds = Dataset(
            scope='group.EIC', detector_version=det_version,
            detector_config=det_config, campaign=campaign,
            physics_tag=physics_tag, evgen_tag=evgen_tag,
            simu_tag=s0, reco_tag=r0, background_tag=background_tag,
            sample_name=row['sample'],
            description=f'PC ingest from a legacy submission line ({row["csv_path"]})',
            metadata={
                'stage': 'evgen',
                'source': {'kind': 'csv_manifest', 'location': row['evgen_path'],
                           'csv_path': row['csv_path']},
                'ingest': {'line': raw.strip(), 'env': env,
                           'target_hours': row['target_hours'],
                           'extra_args': row['extra_args'],
                           'definition': row['definition'],
                           'created_by': created_by},
            },
            created_by=created_by,
        )
        try:
            ds.save()
        except Exception as e:                                  # noqa: BLE001
            raise ServiceError(f'{composed}: {e}')
    row['accepted'] = True
    row['composed_name'] = ds.composed_name
    row['pc'] = ds.physics_config.label if ds.physics_config_id else ''
    row['state'] = 'identified'
    row['edition'] = ds.composed_name
    return row
