"""Derive canonical physics-tag parameters from a catalog task's EVGEN path.

The legacy ``csv_import`` catalog encodes a task's real physics in its path
(``EVGEN/<category>/<sub>/.../<beam>/...``), not in a physics tag — the import
pinned every row to one placeholder anchor tag, so the bound physics (beam,
process) is wrong for almost all of them. This module parses the path into the
parameters a physics tag carries, so each task can be matched to the correct
existing tag, or have the right tag created. See EPICPROD_TASK_CATALOG.md.

The single source of truth is the path. ``derive_physics(path)`` returns the
canonical params; the caller matches/creates the PhysicsTag and rebinds.
"""
import re


#: EXCLUSIVE / SIDIS sub-process folders carry this afterburner-conversion suffix.
_ABCONV = '_ABCONV'


def _strip_abconv(s):
    return s[:-len(_ABCONV)] if s.endswith(_ABCONV) else s


def _beam_pair(beam):
    """'18x275' -> ('18', '275'); '' -> ('N/A', 'N/A')."""
    if beam and 'x' in beam:
        e, h = beam.split('x', 1)
        return (e or 'N/A', h or 'N/A')
    return ('N/A', 'N/A')


# ── physics-tag token vocabulary (scanned at ANY path position) ───────────────
# The catalog paths are not positional: a physics token (q2, species, decay,
# beam-config, ...) can sit at any depth, be compounded (coherent_ep), or live
# in a filename (UPSILON). Derivation recognises tokens by pattern, not slot.
_KNOWN_AREAS = {'SINGLE', 'DIS', 'DDIS', 'SIDIS', 'EXCLUSIVE', 'EW_BSM', 'BACKGROUNDS'}
_BEAM_RE    = re.compile(r'^\d+x\d+$')
_Q2_RE      = re.compile(r'^(minQ2=\d+|q2_\S+)$')
_ION_RE     = re.compile(r'^e(H[0-9]+|He[0-9]+|Li[0-9]*|Ca|Cu|Ru|Pb|Au|C|O)$')
_NUCLEON    = {'ep', 'en'}
_MASS_RE    = re.compile(r'^ma_[0-9]')
_CHANNEL_RE = re.compile(r'^aem')
_UPSILON_RE = re.compile(r'^upsilon(1s|2s|3s)(photo|_threshold)_ab_(hiAcc|hiDiv)_(\d+x\d+)')
_BEAMCONFIG = {'hiAcc', 'hiDiv'}
_DECAY      = {'edecay', 'mudecay'}
_CHARGE     = {'hplus', 'hminus'}
_HELICITY   = {'hel_plus', 'hel_minus'}
_COHERENCE  = {'coherent', 'Coherent'}
_MODEL      = {'bsat'}
_POLAR      = {'unpolarised', 'polarised'}
_FINAL_STATE = {'pi+', 'pi-', 'pi0', 'K+', 'K-', 'K0', 'K+Lambda'}
#: radiation (Rad/noRad) and generator tokens are EVGEN-tag axes — not physics.


def _split_compounds(tok):
    """Yield a segment and its '_'-split parts so embedded physics tokens are
    seen: 'coherent_ep' -> coherent, ep; 'ep_noradcor' -> ep (noradcor ignored)."""
    yield tok
    if '_' in tok:
        yield from tok.split('_')


def _physics_area_segments(path):
    """Return path segments from the first recognised physics area onward,
    dropping any leading prefix — the ``EVGEN`` root, or a past Rucio-DID
    background-overlay chain (``Bkg_*/Synrad_*/GoldC*/<um>/…``). ``[]`` if no
    physics area is present."""
    segs = [s for s in (path or '').split('/') if s]
    for i, s in enumerate(segs):
        if s in _KNOWN_AREAS:
            return segs[i:]
    return []


def _beam_split(tok):
    e, h = tok.split('x', 1)
    return (e or 'N/A', h or 'N/A')


def derive_physics(path, beam=''):
    """Full physics-tag parameter set from an EVGEN path or a past Rucio-DID
    path remainder. Token-scanning, not positional. Returns the schema-named
    param dict, or ``None`` when no physics area is present. Excludes the angle
    range (a sample variant), radiation, and generator (EVGEN-tag axes).

    BACKGROUNDS resolve to process BEAMGAS/SYNRAD so the caller can route them to
    the signal-free p6001 physics tag plus a k background tag.
    """
    segs = _physics_area_segments(path)
    if not segs:
        return None
    area, rest = segs[0], segs[1:]

    if area == 'SINGLE':
        return {'process': 'SINGLE',
                'beam_energy_electron': 'N/A', 'beam_energy_hadron': 'N/A',
                'particle': rest[0] if rest else '',
                'gun_energy': rest[1] if len(rest) > 1 else ''}

    if area == 'BACKGROUNDS':
        e, h = _beam_pair(beam)
        sub = rest[0] if rest else ''
        if sub == 'SYNRAD':
            return {'process': 'SYNRAD', 'beam_energy_electron': e, 'beam_energy_hadron': h}
        return {'process': 'BEAMGAS', 'beam_energy_electron': e, 'beam_energy_hadron': h,
                'bg_source': rest[1] if len(rest) > 1 else '',
                'bg_mechanism': rest[2] if len(rest) > 2 else ''}

    sig = {}
    if area in ('DIS', 'DDIS'):
        proc = 'DDIS' if area == 'DDIS' else 'DIS'
        if 'NC' in rest:
            proc = 'DIS_NC'
        elif 'CC' in rest:
            proc = 'DIS_CC'
        sig['process'] = proc
    elif area == 'SIDIS':
        sub = _strip_abconv(rest[0]) if rest else ''
        sig['process'] = 'SIDIS_' + sub if sub in ('D0', 'DIJET', 'Lc') else 'SIDIS'
    elif area == 'EXCLUSIVE':
        sig['process'] = _strip_abconv(rest[0]) if rest else 'EXCLUSIVE'
    elif area == 'EW_BSM':
        sig['process'] = rest[0] if rest else 'EW_BSM'       # ALP
    else:
        sig['process'] = area

    ions, nucleons = [], []
    for raw in rest:
        m = _UPSILON_RE.match(raw)
        if m:
            sig['state'] = m.group(1)
            sig['mechanism'] = m.group(2).lstrip('_')
            sig['beam_config'] = m.group(3)
            sig['beam_energy_electron'], sig['beam_energy_hadron'] = _beam_split(m.group(4))
            continue
        for tok in _split_compounds(raw):
            if _BEAM_RE.match(tok):
                sig['beam_energy_electron'], sig['beam_energy_hadron'] = _beam_split(tok)
            elif _Q2_RE.match(tok):       sig['q2_range'] = tok
            elif _ION_RE.match(tok):      ions.append(tok)
            elif tok in _NUCLEON:         nucleons.append(tok)
            elif tok in _BEAMCONFIG:      sig['beam_config'] = tok
            elif tok in _DECAY:           sig['decay_mode'] = tok
            elif tok in _CHARGE:          sig['hadron_charge'] = tok
            elif tok in _HELICITY:        sig['helicity'] = tok
            elif tok in _COHERENCE:       sig['coherence'] = 'coherent'
            elif tok in _MODEL:           sig['model'] = tok
            elif tok in _POLAR:           sig['polarization'] = tok
            elif tok in _FINAL_STATE:     sig['final_state'] = tok
            elif _MASS_RE.match(tok):     sig['mass'] = tok
            elif _CHANNEL_RE.match(tok):  sig['channel'] = tok

    if ions:
        sig['beam_species'] = ions[0]
        if nucleons:
            sig['nucleon'] = nucleons[0]
    elif nucleons:
        sig['beam_species'] = nucleons[0]        # bare 'ep' = electron-proton beam
    if 'beam_energy_electron' not in sig:
        if beam and 'x' in beam:
            sig['beam_energy_electron'], sig['beam_energy_hadron'] = _beam_split(beam)
        else:
            sig['beam_energy_electron'] = sig['beam_energy_hadron'] = 'N/A'
    return sig


#: BEAMGAS 4th path segment is a physical mechanism when it is one of these;
#: otherwise it names the generator.
_BG_MECHANISMS = ('brems', 'coulomb', 'touschek')
_BG_BEAM_NXM = re.compile(r'^(\d+)x(\d+)$')
_BG_BEAM_SINGLE = re.compile(r'^(\d+)GeV$')


def _bg_beam(segs, source):
    """Beam energies from a backgrounds path. 'NxM' -> (N, M). A bare 'NGeV' is
    assigned by source: a proton source to the hadron beam, otherwise electron."""
    for s in segs:
        m = _BG_BEAM_NXM.match(s)
        if m:
            return m.group(1), m.group(2)
        m = _BG_BEAM_SINGLE.match(s)
        if m:
            return ('N/A', m.group(1)) if source == 'proton' else (m.group(1), 'N/A')
    return 'N/A', 'N/A'


def derive_background(path):
    """Canonical background (k) tag params from a stripped EVGEN/BACKGROUNDS path,
    or None if the path is not a backgrounds entry.

    BEAMGAS: ``EVGEN/BACKGROUNDS/BEAMGAS/<source>/<mechanism-or-generator>/…/<beam>/…``.
    The 4th segment is a physical mechanism (brems/coulomb/touschek) when it is
    one, otherwise it is the generator. SYNRAD has no source or mechanism. All
    values are open strings — the parser passes through whatever the path names.
    Always returns the full set of match fields (blank where not present) so the
    tag-dedup lookup is consistent.
    """
    segs = (path or '').split('/')
    if len(segs) < 3 or segs[0] != 'EVGEN' or segs[1] != 'BACKGROUNDS':
        return None
    sub = segs[2]
    params = {
        'background_type': sub,
        'bg_source': '', 'bg_mechanism': '', 'bg_generator': '',
        'beam_energy_electron': 'N/A', 'beam_energy_hadron': 'N/A',
    }
    if sub == 'SYNRAD':
        if len(segs) > 3:
            params['bg_generator'] = segs[3]
    else:
        source = segs[3] if len(segs) > 3 else ''
        params['bg_source'] = source
        seg4 = segs[4] if len(segs) > 4 else ''
        if seg4 in _BG_MECHANISMS:
            params['bg_mechanism'] = seg4
            gen_parts = [s for s in segs[5:7]
                         if s and not _BG_BEAM_NXM.match(s) and not _BG_BEAM_SINGLE.match(s)]
            params['bg_generator'] = '/'.join(gen_parts)
        else:
            params['bg_generator'] = seg4
    e, h = _bg_beam(segs, params['bg_source'])
    params['beam_energy_electron'] = e
    params['beam_energy_hadron'] = h
    return params


#: Generator family names whose version follows the name in a token. pythia is
#: handled separately because its family carries the major-version digit
#: (pythia8 / pythia6).
_EVGEN_NAMES = ('EpIC', 'BeAGLE', 'sartre', 'eSTARlight', 'eicMesonSFGen',
                'lAger', 'rapgap', 'DEMPgen', 'DJANGOH', 'GETaLM')
_PYTHIA_RE = re.compile(r'^[Pp]ythia[ _]?(\d)(.*)$')


def _split_gen_token(tok):
    """Split a '<Generator><Version>' token into (generator, generator_version),
    or (None, None) when there is no known generator *with a non-empty version*.

    A bare generator name (e.g. 'pythia8', 'eSTARlight') has no version in the
    source and resolves to (None, None) — left for manual association, not
    guessed. The leading 'v' of a version is preserved (EpIC 'v1.1.6-1.2');
    pythiaN keeps its major-version digit in both the family and the version.
    """
    t = (tok or '').strip()
    if not t:
        return None, None
    m = _PYTHIA_RE.match(t)
    if m:
        if not m.group(2).strip(' ._-'):     # bare 'pythia8' / 'Pythia 8'
            return None, None
        return f'pythia{m.group(1)}', f'{m.group(1)}{m.group(2)}'.lstrip('._- ')
    for g in _EVGEN_NAMES:
        if t.lower().startswith(g.lower()):
            ver = t[len(g):].lstrip('._- ')   # keep a leading 'v'
            return (g, ver) if ver else (None, None)
    return None, None


def derive_evgen(path, gen_version=''):
    """Curated (generator, generator_version) for a catalog row, or None when no
    confident resolution exists — left for manual association, never guessed.

    - ``EVGEN/SINGLE/...`` samples are the particle gun.
    - A background ``dataprod_rel`` release names no generator; the generator is
      the repository (e.g. EIC_ESR_Xsuite, EIC_SR_Geant4).
    - Otherwise a '<Generator><Version>' token from the gen_version release tag
      or the path is split; a bare generator with no version resolves to None.
    """
    segs = (path or '').split('/')
    if len(segs) > 1 and segs[0] == 'EVGEN' and segs[1] == 'SINGLE':
        return {'generator': 'particle_gun', 'generator_version': ''}
    # Radiative corrections are a generator run-mode that genuinely changes the
    # physics, so they belong in the evgen tag (not a sample discriminator): a
    # 'Rad' / 'noRad' path segment splits an otherwise-identical generator into
    # distinct tags. Only set when the path names it; absent elsewhere.
    radiative = ''
    if 'noRad' in segs:
        radiative = 'off'
    elif 'Rad' in segs:
        radiative = 'on'

    def _with_rad(params):
        if radiative:
            params['radiative'] = radiative
        return params

    gv = (gen_version or '').strip()
    # PYTHIA-RAD-CORR releases are bare versions and the path's pythia6 segment
    # is not a clean generator+version — ambiguous, left for manual association.
    if 'pythia-rad-corr' in gv.lower():
        return None
    release = gv.rstrip('/').split('/')[-1] if gv else ''
    if release.startswith('dataprod_rel') and 'github.com/' in gv:
        repo = gv.split('github.com/', 1)[1].split('/releases', 1)[0].split('/')[-1]
        if repo:
            return _with_rad({'generator': repo, 'generator_version': release})
    for tok in [release, gv, *segs]:
        g, v = _split_gen_token(tok)
        if g:
            return _with_rad({'generator': g, 'generator_version': v})
    return None


def single_particle_angle(path):
    """Angular-range tail of a single-particle path, or '' if none.

    Single-particle samples share a ``(particle, gun_energy)`` physics tag but
    differ by polar-angle range; the angle is a per-task detail, not part of the
    reusable tag (``derive_physics`` deliberately omits it). This returns the
    path tail after ``EVGEN/SINGLE/<particle>/<energy>/`` so the importer can
    store it as a per-task override. Returns '' for a non-single-particle path.
    """
    segs = (path or '').split('/')
    if len(segs) < 2 or segs[0] != 'EVGEN' or segs[1] != 'SINGLE':
        return ''
    return '/'.join(segs[4:])
