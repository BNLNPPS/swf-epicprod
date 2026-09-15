"""The path reader's two-beam polarization axis (EPICPROD_EVGEN_INPUTS.md).

Pure: derive_physics over path strings, no database.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from pcs.physics_match import derive_evgen, derive_physics  # noqa: E402

DJANGOH = 'EVGEN/DIS/DJANGOH4.6.10-2.0/CC/Rad/%s/9x275/%s'


def test_polarization_token_read_with_species():
    sig = derive_physics(DJANGOH % ('eMinus-pMinus', 'q2_100to1000'), beam='9x275')
    assert sig['process'] == 'DIS_CC'
    assert sig['beam_polarization'] == 'eMinus-pMinus'
    assert sig['beam_species'] == 'ep'
    assert (sig['beam_energy_electron'], sig['beam_energy_hadron']) == ('9', '275')
    assert sig['q2_range'] == 'q2_100to1000'


def test_twins_derive_distinct_physics():
    minus = derive_physics(DJANGOH % ('eMinus-pMinus', 'q2_3000to9000'), beam='9x275')
    plus = derive_physics(DJANGOH % ('eMinus-pPlus', 'q2_3000to9000'), beam='9x275')
    assert minus != plus
    assert {k for k in minus if minus[k] != plus.get(k)} == {'beam_polarization'}


def test_paths_without_the_token_are_unchanged():
    sig = derive_physics('EVGEN/DIS/pythia8.316-1.0/NC/noRad/ep/10x100/q2_1to10', beam='10x100')
    assert 'beam_polarization' not in sig
    assert sig['beam_species'] == 'ep'


UPSILON = 'EVGEN/EXCLUSIVE/UPSILON_ABCONV/eSTARlight1.3.0-1.0/%s/%s/q2_0to0.01/%s'


def test_upsilon_state_read_from_directory_segment():
    sig = derive_physics(UPSILON % ('Upsilon3S', '9x275', 'hiDiv'), beam='9x275')
    assert sig['process'] == 'UPSILON'
    assert sig['state'] == '3s'
    assert sig['beam_config'] == 'hiDiv'
    assert (sig['beam_energy_electron'], sig['beam_energy_hadron']) == ('9', '275')


def test_upsilon_states_derive_distinct_physics():
    one = derive_physics(UPSILON % ('Upsilon1S', '9x130', 'hiAcc'), beam='9x130')
    two = derive_physics(UPSILON % ('Upsilon2S', '9x130', 'hiAcc'), beam='9x130')
    assert {k for k in one if one[k] != two.get(k)} == {'state'}


def test_afterburner_preset_from_abconv_path():
    ev = derive_evgen(UPSILON % ('Upsilon3S', '9x275', 'hiDiv'))
    assert ev == {'generator': 'eSTARlight', 'generator_version': '1.3.0-1.0',
                  'afterburner_preset': 'ip6_hiDiv_275x9'}
    ev = derive_evgen(UPSILON % ('Upsilon1S', '9x130', 'hiAcc'))
    assert ev['afterburner_preset'] == 'ip6_ep_130x9'


def test_no_preset_without_abconv_in_the_path():
    ev = derive_evgen('EVGEN/DIS/DJANGOH4.6.10-2.0/CC/Rad/eMinus-pMinus/9x275/q2_100to1000')
    assert 'afterburner_preset' not in ev
    assert ev['radiative'] == 'on'
