"""The output datasets and metadata composed from an EVGEN spec
(swf_epicprod.output_datasets is pure)."""
import unittest

from swf_epicprod.output_datasets import (
    dataset_metadata,
    geometry_config,
    output_datasets,
    output_tag,
    software_release,
)

ENV = {'COPYRECO': 'true', 'COPYFULL': 'false', 'COPYLOG': 'true',
       'USERUCIO': 'true', 'OUT_RSE': 'EIC-XRD',
       'DETECTOR_VERSION': '26.07.1', 'DETECTOR_CONFIG': 'epic_craterlake',
       'EBEAM': '10', 'PBEAM': '100'}
ROW = 'DIS/pythia8.316-1.0/NC/noRad/ep/10x100/q2_1to10/pythia8NCDIS_10x100_minQ2=1_beamEffects_xAngle=-0.025_hiDiv_1,hepmc3.tree.root,1000,0000'
CFG = {'jug_xl_tag': '26.07.1-stable'}


def spec(env=ENV, rows=(ROW,), **extra):
    s = {'env': dict(env), 'csvRows': list(rows)}
    s.update(extra)
    return s


class NamesTest(unittest.TestCase):
    def test_tag_follows_run_sh(self):
        self.assertEqual(output_tag(ENV, 'DIS/pythia8.316-1.0/NC/noRad/ep/10x100/q2_1to10/x'),
                         '26.07.1/epic_craterlake/DIS/pythia8.316-1.0/NC/noRad/ep/10x100/q2_1to10')
        env = dict(ENV, TAG_PREFIX='try2')
        self.assertEqual(output_tag(env, 'DIS/a/b/x'), '26.07.1/epic_craterlake/try2/DIS/a/b')

    def test_levels_follow_the_copy_flags(self):
        out = output_datasets(spec(), CFG)
        self.assertEqual([(o['level'], o['dataset']) for o in out],
                         [('RECO', '/RECO/26.07.1/epic_craterlake/DIS/pythia8.316-1.0/NC/noRad/ep/10x100/q2_1to10')])
        out = output_datasets(spec(dict(ENV, COPYFULL='true')), CFG)
        self.assertEqual([o['level'] for o in out], ['FULL', 'RECO'])

    def test_one_dataset_per_directory(self):
        rows = [ROW, ROW.replace('_1,', '_2,'), ROW.replace('q2_1to10', 'q2_10to100')]
        out = output_datasets(spec(rows=rows), CFG)
        self.assertEqual(len(out), 2)

    def test_trial_root_and_internal_evgen(self):
        env = dict(ENV, EVGEN_INTERNAL='true', COPYEVGEN='true')
        out = output_datasets(spec(env, trial={'outputRoot': 'TEST/trial/t7'}), CFG)
        self.assertEqual([o['dataset'] for o in out],
                         ['/TEST/trial/t7/RECO/26.07.1/epic_craterlake/DIS/pythia8.316-1.0/NC/noRad/ep/10x100/q2_1to10',
                          '/TEST/trial/t7/EVGEN/DIS/pythia8.316-1.0/NC/noRad/ep/10x100/q2_1to10'])
        self.assertIsNone(out[1]['metadata'])  # a generated sample registers without metadata


class MetadataTest(unittest.TestCase):
    def test_reco_metadata_matches_the_job_reading(self):
        md = dataset_metadata(spec(), CFG, 'RECO', ROW.split(',')[0], 'hepmc3.tree.root')
        self.assertEqual(md, {
            'software_release': '26.07.1-stable', 'generator': 'pythia8',
            'is_background_mixed': False, 'requester_pwg': 'inclusive',
            'q2_min_gev2': 1, 'q2_max_gev2': 10, 'data_level': 'reconstruction',
            'geometry_config': 'craterlake_10x100',
            'electron_beam_energy_gev': 10, 'ion_beam_energy_gev': 100, 'ion_species': 'p'})

    def test_ion_beam_and_background(self):
        env = dict(ENV, PBEAM='41_Au197', EBEAM='5', BG_FILES='bg1.json')
        md = dataset_metadata(spec(env), CFG, 'FULL', 'DIS/BeAGLE1.03.02-1.0/eAu/5x41/q2_1to10/x', 'hepmc3.tree.root')
        self.assertEqual(md['geometry_config'], 'craterlake_5x41_Au197')
        self.assertEqual(md['ion_species'], 'Au197')
        self.assertTrue(md['is_background_mixed'])
        self.assertEqual(md['requester_dsc'], 'tracking')
        self.assertEqual(md['data_level'], 'simulation')

    def test_single_particle_declares_no_beams_or_gun(self):
        md = dataset_metadata(spec(), CFG, 'RECO', 'SINGLE/pi+/100MeV/45to135deg/x', 'steer')
        self.assertEqual(md['generator'], 'single_particle')
        self.assertNotIn('requester_pwg', md)
        self.assertNotIn('electron_beam_energy_gev', md)
        self.assertNotIn('gun_particle', md)

    def test_stand_in_beams_name_the_geometry(self):
        self.assertEqual(geometry_config(dict(ENV, DETECTOR_BEAMS='10x100')), 'craterlake_10x100')
        self.assertEqual(geometry_config(dict(ENV, EBEAM='9', PBEAM='130', DETECTOR_BEAMS='10x130')),
                         'craterlake_10x130')

    def test_software_release_reads_as_eic_info(self):
        self.assertEqual(software_release({'jug_xl_tag': '26.07.1-stable'}), '26.07.1-stable')
        self.assertEqual(software_release({'container_image': '/cvmfs/x/eic_xl:nightly'}), 'nightly')
        self.assertEqual(software_release({'jug_xl_tag': '25.10.0-stable-abc'}), '25.10.0-stable')


if __name__ == '__main__':
    unittest.main()
