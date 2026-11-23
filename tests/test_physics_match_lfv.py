"""The path reader's channel axis for lepton-flavour-violating samples
(EW_BSM/LFV): the channel directory is part of the physics identity, as it
is for the ALP samples.

Pure: derive_physics over path strings, no database.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from pcs.physics_match import derive_evgen, derive_physics  # noqa: E402

LFV = 'EVGEN/EW_BSM/LFV/pythia8.307-1.0/%s/18x275/q2_100to2500'


def test_lfv_channel_read():
    sig = derive_physics(LFV % 'etau', beam='18x275')
    assert sig == {'process': 'LFV', 'channel': 'etau',
                   'beam_energy_electron': '18', 'beam_energy_hadron': '275',
                   'q2_range': 'q2_100to2500'}
    assert derive_evgen(LFV % 'etau', '') == {'generator': 'pythia8',
                                              'generator_version': '8.307-1.0'}


def test_lfv_channels_derive_distinct_physics():
    etau = derive_physics(LFV % 'etau', beam='18x275')
    emu = derive_physics(LFV % 'emu', beam='18x275')
    assert {k for k in etau if etau[k] != emu.get(k)} == {'channel'}


def test_channel_token_is_read_under_lfv_only():
    for path in ('EVGEN/EW_BSM/ALP/madgraph5-3.7.0-1.0/etau/10x110/ma_1.0',
                 'EVGEN/DIS/NC/etau/18x275/q2_100to2500',
                 'EVGEN/EXCLUSIVE/DVCS/emu/18x275'):
        assert 'channel' not in derive_physics(path, beam='')


def test_alp_channel_unchanged():
    sig = derive_physics('EVGEN/EW_BSM/ALP/madgraph5-3.7.0-1.0/aem-axem/10x110/ma_1.0', beam='')
    assert (sig['process'], sig['channel'], sig['mass']) == ('ALP', 'aem-axem', 'ma_1.0')
