"""The path reader's Bjorken-x axis and named pythia builds
(pythia6-lhapdf 1.1.0, 2026-10-27).

Pure: derive_physics / derive_evgen over path strings, no database.
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from pcs.physics_match import derive_evgen, derive_physics, invalid_x_window  # noqa: E402

BASE = 'EVGEN/SIDIS/pythia6-lhapdf/1.1.0/%s/%s/%s'
GROUPS = (('ep_noradcor', '9x130'), ('eAu_EPPS16_noradcor', '9x100'),
          ('eAg_EPPS16_noradcor', '9x115'))
BINS = ('x_1e-4to1e-3', 'x_1e-3to1e-2', 'x_1e-2to1e-1')


def test_x_bin_read_into_x_range():
    sig = derive_physics(BASE % ('ep_noradcor', '9x130', 'x_1e-3to1e-2'))
    assert sig['process'] == 'SIDIS'
    assert sig['beam_species'] == 'ep'
    assert (sig['beam_energy_electron'], sig['beam_energy_hadron']) == ('9', '130')
    assert sig['x_range'] == 'x_1e-3to1e-2'


def test_x_bins_derive_distinct_physics():
    for species, beam in GROUPS:
        sigs = [derive_physics(BASE % (species, beam, b)) for b in BINS]
        assert len({tuple(sorted(s.items())) for s in sigs}) == 3
        for a in sigs[1:]:
            assert {k for k in a if a[k] != sigs[0].get(k)} == {'x_range'}


def test_plain_decimal_x_window():
    sig = derive_physics('EVGEN/EXCLUSIVE/MESON_SF/ep/10x100/x_0.001to1')
    assert sig['x_range'] == 'x_0.001to1'


def test_paths_without_the_token_are_unchanged():
    sig = derive_physics('EVGEN/DIS/pythia8.316-1.0/NC/noRad/ep/10x100/q2_1to10', beam='10x100')
    assert 'x_range' not in sig
    assert sig['q2_range'] == 'q2_1to10'


def test_named_pythia_build_takes_the_next_segment_as_version():
    for species, beam in GROUPS:
        for b in BINS:
            assert derive_evgen(BASE % (species, beam, b)) == {
                'generator': 'pythia6-lhapdf', 'generator_version': '1.1.0'}


def test_pythia8_release_token_unchanged():
    evgen = derive_evgen('EVGEN/DIS/pythia8.316-1.0/NC/noRad/ep/10x100/q2_1to10')
    assert evgen['generator'] == 'pythia8'
    assert evgen['generator_version'] == '8.316-1.0'
    assert evgen['radiative'] == 'off'


BAD_X = ('x_..to++', 'x_1e-to1e-', 'x_1e-2to1e-3', 'x_0.1to0.1', 'x_0.5to2', 'x_')


def test_malformed_or_unordered_x_window_refuses_the_path():
    # Read without its bin the path would be the binless sample.
    for tok in BAD_X:
        assert invalid_x_window(BASE % ('ep_noradcor', '9x130', tok)) == tok
        assert derive_physics(BASE % ('ep_noradcor', '9x130', tok)) is None, tok


def test_registered_sample_refuses_a_bad_x_window_with_its_reason(monkeypatch):
    # The DB-backed helpers are stubbed: the refusal comes before any lookup.
    for name in ('django', 'django.db', 'pcs.ingest', 'pcs.models', 'pcs.services'):
        monkeypatch.setitem(sys.modules, name, mock.MagicMock())
    monkeypatch.delitem(sys.modules, 'pcs.registered_samples', raising=False)
    from pcs.registered_samples import derive_registered_sample
    for tok in BAD_X:
        identity, reason = derive_registered_sample(
            'epic:/' + BASE % ('ep_noradcor', '9x130', tok))
        assert identity is None, tok
        assert 'invalid x window' in reason and repr(tok) in reason


def test_exponent_with_plus_sign():
    sig = derive_physics(BASE % ('ep_noradcor', '9x130', 'x_1e-1to1E+0'))
    assert sig['x_range'] == 'x_1e-1to1E+0'


def test_named_build_in_the_release_tag():
    evgen = derive_evgen(BASE % ('ep_noradcor', '9x130', BINS[0]),
                         gen_version='pythia6-lhapdf/1.1.0')
    assert evgen == {'generator': 'pythia6-lhapdf', 'generator_version': '1.1.0'}


def test_pythia6_eic_reading_unchanged():
    # Bound PCs derive from this reading; it changes only with their repair.
    evgen = derive_evgen('EVGEN/SIDIS/pythia6-eic/1.3.0/ep_noradcor/18x275/q2_0to1')
    assert evgen == {'generator': 'pythia6', 'generator_version': '6-eic'}
