"""The particle gun (docs/EPICPROD_INTERNAL_EVGEN.md § Single-particle
guns): the PCS resolver, environment, layout and manifest, and the
payload's steering, which must match the production team's hand-written
files byte for byte."""
import os
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace

from pcs.commands import (GUN_DEFAULT_BEAMS, _evgen_manifest_internal,
                          _internal_evgen_env, gun_config,
                          internal_evgen_sample, is_particle_gun,
                          payload_beams)

PAYLOAD = os.path.join(os.path.dirname(__file__), '..', 'swf_epicprod',
                       'payload', 'evgen_generate.py')

# The volatile area's EVGEN/SINGLE/pi+/1GeV/3to50deg/pi+_1GeV_3to50deg.steer
# and EVGEN/SINGLE/e-/2GeV/etaScan/e-_2GeV_eta-1.7.steer, read 2026-10-24.
ANGLE_FILE = '''from DDSim.DD4hepSimulation import DD4hepSimulation
from g4units import mm, GeV, MeV, degree
SIM = DD4hepSimulation()
SIM.gun.energy = 1*GeV
SIM.gun.particle = "pi+"
SIM.gun.position = (0.0, 0.0, 0.0)
SIM.gun.direction = (0.0, 0.0, 1.0)
SIM.gun.distribution = "cos(theta)"
SIM.gun.thetaMin = 3*degree
SIM.gun.thetaMax = 50*degree
'''
ETA_FILE = '''from DDSim.DD4hepSimulation import DD4hepSimulation
from g4units import mm, GeV, MeV, degree
SIM = DD4hepSimulation()
SIM.gun.distribution = "uniform"
SIM.gun.energy = 2*GeV
SIM.gun.particle = "e-"
SIM.gun.position = (0.0, 0.0, 0.0)
SIM.gun.etaMin = -1.7
SIM.gun.etaMax = -1.7
'''


def task(particle='pi+', energy='1GeV', sample='3to50deg', generator='particle_gun',
         target=None, source='', trial=False):
    physics = {'process': 'SINGLE', 'particle': particle, 'gun_energy': energy,
               'beam_energy_electron': 'N/A', 'beam_energy_hadron': 'N/A'}
    ds = SimpleNamespace(
        physics_tag_id=1, physics_tag=SimpleNamespace(parameters=physics),
        evgen_tag_id=1, evgen_tag=SimpleNamespace(
            parameters={'generator': generator, 'generator_version': ''}),
        sample_name=sample, expected_events=target, expected_events_source=source,
        metadata={'trial': 1} if trial else {})
    return SimpleNamespace(dataset_id=1, dataset=ds)


class Resolver(unittest.TestCase):

    def test_angle_range(self):
        t = task()
        self.assertTrue(is_particle_gun(t))
        self.assertEqual(gun_config(t)['energy_g4'], '1*GeV')
        self.assertEqual(_internal_evgen_env(t), {
            'EVGEN_INTERNAL': 'true', 'EVGEN_GENERATOR': 'particle_gun',
            'EVGEN_PARTICLE': 'pi+', 'EVGEN_GUN_ENERGY': '1*GeV', 'COPYEVGEN': 'false',
            'EVGEN_GUN_THETA_MIN': '3', 'EVGEN_GUN_THETA_MAX': '50'})
        self.assertEqual(internal_evgen_sample(t),
                         ('SINGLE/pi+/1GeV/3to50deg', 'pi+_1GeV_3to50deg'))

    def test_eta_point(self):
        t = task(particle='e-', energy='100MeV', sample='eta-1.7')
        env = _internal_evgen_env(t)
        self.assertEqual((env['EVGEN_GUN_ENERGY'], env['EVGEN_GUN_ETA']), ('100*MeV', '-1.7'))
        self.assertNotIn('EVGEN_GUN_THETA_MIN', env)
        self.assertEqual(internal_evgen_sample(t)[1], 'e-_100MeV_eta-1.7')

    def test_refusals(self):
        for kwargs in ({'energy': '1 GeV'}, {'energy': '0GeV'}, {'sample': 'etaScan'},
                       {'sample': '50to3deg'}, {'sample': '3to190deg'},
                       {'particle': 'pi"+'}, {'particle': ''}):
            with self.subTest(**kwargs), self.assertRaises(ValueError):
                gun_config(task(**kwargs))

    def test_no_beams(self):
        self.assertEqual(payload_beams(task()), ('', '', ''))
        self.assertEqual(GUN_DEFAULT_BEAMS, '5x41')
        self.assertFalse(is_particle_gun(task(generator='pythia8')))


class Manifest(unittest.TestCase):

    def test_rows_sum_to_target(self):
        rows = _evgen_manifest_internal(task(target=1_000_000, source='included'), 3000)
        self.assertEqual(len(rows), 334)
        self.assertEqual(rows[0], 'SINGLE/pi+/1GeV/3to50deg/pi+_1GeV_3to50deg,steer,3000,0000')
        self.assertEqual(rows[-1], 'SINGLE/pi+/1GeV/3to50deg/pi+_1GeV_3to50deg,steer,1000,0333')
        self.assertEqual(sum(int(r.split(',')[2]) for r in rows), 1_000_000)

    def test_target_needed(self):
        for target, source in ((None, ''), (1_000_000, 'derived'), (0, 'included')):
            with self.subTest(target=target, source=source), self.assertRaises(ValueError):
                _evgen_manifest_internal(task(target=target, source=source), 3000)
        # 100 per job is exactly the 10,000 chunks 0000 to 9999; 99 is past them.
        rows = _evgen_manifest_internal(task(target=1_000_000, source='included'), 100)
        self.assertEqual((len(rows), rows[-1].rsplit(',', 1)[1]), (10000, '9999'))
        with self.assertRaises(ValueError):
            _evgen_manifest_internal(task(target=1_000_000, source='included'), 99)

    def test_trial_one_row(self):
        self.assertEqual(_evgen_manifest_internal(task(trial=True), 50),
                         ['SINGLE/pi+/1GeV/3to50deg/pi+_1GeV_3to50deg,steer,50,0000'])


class Steering(unittest.TestCase):

    def compose(self, env, name):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, name)
            run = subprocess.run(
                [sys.executable, '-I', PAYLOAD, '--out', out, '--events', '10',
                 '--workdir', os.path.join(tmp, 'work')],
                env=dict(os.environ, EVGEN_GENERATOR='particle_gun', **env),
                capture_output=True, text=True)
            return run.returncode, (open(out).read() if os.path.exists(out) else None)

    def test_byte_for_byte(self):
        on = {}  # on by default since 0.25.1
        self.assertEqual(self.compose(dict(on, EVGEN_PARTICLE='pi+', EVGEN_GUN_ENERGY='1*GeV',
                                           EVGEN_GUN_THETA_MIN='3', EVGEN_GUN_THETA_MAX='50'),
                                      'pi+_1GeV_3to50deg.steer'), (0, ANGLE_FILE))
        self.assertEqual(self.compose(dict(on, EVGEN_PARTICLE='e-', EVGEN_GUN_ENERGY='2*GeV',
                                           EVGEN_GUN_ETA='-1.7'),
                                      'e-_2GeV_eta-1.7.steer'), (0, ETA_FILE))

    def test_switch_off_refuses(self):
        rc, written = self.compose({'EPICPROD_SWITCH_PARTICLE_GUN': '0', 'EVGEN_PARTICLE': 'e-',
                                    'EVGEN_GUN_ENERGY': '2*GeV', 'EVGEN_GUN_ETA': '-1.7'}, 'x.steer')
        self.assertEqual((rc != 0, written), (True, None))


if __name__ == '__main__':
    unittest.main()
