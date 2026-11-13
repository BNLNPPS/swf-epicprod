"""The card rules of analysis runs (analysis/cards.py, docs/ANALYSIS.md):
which file of a work unit a dataset card keeps, what a run card hashes and
what it leaves out, how a run splits into jobs, and where it writes."""
import unittest

from analysis import cards

SAMPLE = 'RECO/26.07.3/epic_craterlake/SIDIS/pythia6-lhapdf/1.1.0/ep_noradcor/9x130/x_1e-2to1e-1'
TRY2 = SAMPLE.replace('epic_craterlake/', 'epic_craterlake/try2/')
STEM = 'pythia6-lhapdf_1.1.0_ep_noradcor_9x130_x_1e-2to1e-1_run001.ab'

CARD = {
    'identifier': 'epic:benchmark:SIDIS/dihadron',
    'eic_shell': {'container': 'eic_xl', 'version': '26.07.3-stable'},
    'parameters': {'DIHADRON_TRIG_LOW': 2, 'MODE': 'reco'},
    'split': {'files_per_job': 2},
    'stages': {'map': 'snakemake m', 'merge': 'snakemake g', 'finalize': 'snakemake f'},
    'files': {'list': 'l/$SAMPLE/list_$JOB.txt', 'partial': 'p/$SAMPLE/job_$JOB.root',
              'hists': 'p/$SAMPLE/hists.json', 'plots': 'p/$SAMPLE/plots'},
}
TAG = {'label': 'a1', 'repo': 'https://github.com/eic/physics_benchmarks',
       'commit': '8ccc766ecdb7d2b408d882630248ba77ea357a73',
       'card_path': 'benchmarks/SIDIS/dihadron/card.yml', 'card_sha256': 'c' * 64,
       'image': '/cvmfs/singularity.opensciencegrid.org/eicweb/eic_xl:26.07.3-stable'}


def did(sample, unit):
    return f'/{sample}/{STEM}.{unit}.eicrecon.edm4eic.root'


class DatasetCardTest(unittest.TestCase):

    def test_latest_try_wins_per_unit(self):
        rows = cards.manifest_rows([
            {'did': did(SAMPLE, '0001.0001'), 'events': 100},
            {'did': did(TRY2, '0001.0001'), 'events': 90},
            {'did': did(SAMPLE, '0002.0001'), 'events': 80},
        ])
        self.assertEqual([r['did'] for r in rows],
                         [did(TRY2, '0001.0001'), did(SAMPLE, '0002.0001')])
        self.assertEqual([r['try'] for r in rows], [2, 1])

    def test_diverted_copy_is_the_same_unit(self):
        diverted = f'/{SAMPLE}/{STEM}.0001.0001.p41354.eicrecon.edm4eic.root'
        self.assertEqual(cards.work_unit(diverted), cards.work_unit(did(SAMPLE, '0001.0001')))

    def test_rows_do_not_depend_on_input_order(self):
        files = [{'did': did(SAMPLE, f'{k:04d}.0001'), 'events': k} for k in range(5)]
        self.assertEqual(cards.manifest_rows(files), cards.manifest_rows(files[::-1]))

    def test_producer_unit_key_is_used_when_given(self):
        rows = cards.manifest_rows([{'did': did(SAMPLE, 'a'), 'unit': 'u1', 'try': 1},
                                    {'did': did(TRY2, 'b'), 'unit': 'u1', 'try': 2}])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['did'], did(TRY2, 'b'))

    def test_header_and_blob_round_trip(self):
        rows = cards.manifest_rows([{'did': did(SAMPLE, '0001.0001'), 'events': 7,
                                     'bytes': 9, 'adler32': 'ABCD'}])
        header = cards.dataset_card(SAMPLE, 1, rows, 'delivered', '2026-11-13T00:00:00')
        self.assertEqual(header['events'], 7)
        self.assertEqual(header['manifest_sha256'], cards.canonical_sha256(rows))
        self.assertEqual(rows[0]['adler32'], '0000abcd')
        blob = cards.manifest_blob(rows)
        self.assertEqual(blob, cards.manifest_blob(rows))  # equal rows, equal bytes
        self.assertEqual(cards.read_manifest_blob(blob), rows)

    def test_card_refuses_other_samples(self):
        rows = cards.manifest_rows([{'did': did(SAMPLE, '1')},
                                    {'did': did(SAMPLE.replace('9x130', '18x275'), '1')}])
        with self.assertRaises(cards.CardError):
            cards.dataset_card(SAMPLE, 1, rows, 'delivered', 'now')

    def test_unknown_events_make_the_total_unknown(self):
        rows = cards.manifest_rows([{'did': did(SAMPLE, '1'), 'events': 5},
                                    {'did': did(SAMPLE, '2')}])
        self.assertIsNone(cards.dataset_card(SAMPLE, 1, rows, 'rucio', 'now')['events'])


class AnalysisCardTest(unittest.TestCase):

    def test_benchmark_shaped_card_passes(self):
        self.assertIs(cards.check_analysis_card(CARD), CARD)

    def test_every_problem_is_named(self):
        with self.assertRaises(cards.CardError) as e:
            cards.check_analysis_card({'identifier': 'x', 'parameters': {'bad-name': 1}})
        for word in ('eic_shell', 'stages', 'files_per_job', 'shell names', 'files.list', 'files.plots'):
            self.assertIn(word, str(e.exception))

    def test_commit_must_be_full(self):
        with self.assertRaises(cards.CardError):
            cards.check_commit('8ccc766')
        self.assertEqual(cards.check_commit(TAG['commit'].upper()), TAG['commit'])


class RunCardTest(unittest.TestCase):

    def card(self, **kw):
        args = dict(tag=TAG, inputs=[{'sample': SAMPLE, 'revision': 1, 'manifest_sha256': 'm' * 64}],
                    exclusions=[], parameters={'MODE': 'reco'}, split={'files_per_job': 2})
        args.update(kw)
        return cards.run_card(**args)

    def test_same_choices_same_id(self):
        a = self.card(exclusions=[did(SAMPLE, '2'), did(SAMPLE, '1')])
        b = self.card(exclusions=[did(SAMPLE, '1').lstrip('/'), did(SAMPLE, '2')])
        self.assertEqual(cards.run_card_id(a), cards.run_card_id(b))

    def test_each_choice_changes_the_id(self):
        base = cards.run_card_id(self.card())
        for change in (dict(exclusions=[did(SAMPLE, '1')]),
                       dict(parameters={'MODE': 'truth'}),
                       dict(split={'files_per_job': 3}),
                       dict(tag=dict(TAG, commit='0' * 40))):
            self.assertNotEqual(cards.run_card_id(self.card(**change)), base, change)

    def test_one_campaign_per_run(self):
        other = SAMPLE.replace('26.07.3', '26.07.1')
        with self.assertRaises(cards.CardError):
            self.card(inputs=[{'sample': SAMPLE, 'revision': 1, 'manifest_sha256': 'm'},
                              {'sample': other, 'revision': 1, 'manifest_sha256': 'm'}])

    def test_overrides_must_be_declared(self):
        self.assertEqual(cards.effective_parameters({'A': 1}, {'A': 2}), {'A': 2})
        with self.assertRaises(cards.CardError):
            cards.effective_parameters({'A': 1}, {'B': 2})

    def test_params_string_is_shell_safe(self):
        self.assertEqual(cards.params_string({'B': False, 'A': 2}), 'A=2 B=false')
        for bad in ('two words', 'x;rm', '$(id)', ''):
            with self.assertRaises(cards.CardError):
                cards.params_string({'A': bad})


class SplitAndStorageTest(unittest.TestCase):

    def test_jobs_follow_samples_and_skip_exclusions(self):
        other = SAMPLE.replace('9x130', '18x275')
        rows_a = cards.manifest_rows([{'did': did(SAMPLE, str(k))} for k in range(5)])
        rows_b = cards.manifest_rows([{'did': did(other, str(k))} for k in range(2)])
        jobs = cards.split_jobs([(SAMPLE, rows_a), (other, rows_b)],
                                [did(SAMPLE, '1')], 2)
        self.assertEqual([j['seq'] for j in jobs], [0, 1, 2])
        self.assertEqual([j['job'] for j in jobs], ['0000', '0001', '0000'])
        self.assertEqual([len(j['dids']) for j in jobs], [2, 2, 2])
        self.assertNotIn(did(SAMPLE, '1'), jobs[0]['dids'] + jobs[1]['dids'])
        self.assertEqual(jobs[0]['label'], 'ep_noradcor_9x130_x_1e-2to1e-1')
        self.assertEqual(cards.parse_job_row(cards.job_row(jobs[2])),
                         {k: jobs[2][k] for k in ('seq', 'label', 'job', 'dids')})

    def test_campaign_and_paths(self):
        self.assertEqual(cards.campaign_of(SAMPLE), '26073')
        rid = 'f' * 64
        self.assertEqual(cards.scratch_did('user', 'rahmans', rid, 'x.root'),
                         '/rahmans/scratch/ffffffffffff/x.root')
        self.assertEqual(cards.publish_did('validation', 'rahmans', '26073', 'p.png'),
                         '/validation/images/campaigns/26073/p.png')
        with self.assertRaises(cards.CardError):
            cards.publish_root('user', '../etc')

    def test_histogram_digest_ignores_order_of_names_only(self):
        a = cards.histogram_digests({'h': {'entries': 3, 'bins': [0, 1, 2, 0]}})
        b = cards.histogram_digests({'h': {'entries': 3, 'bins': [0, 2, 1, 0]}})
        self.assertNotEqual(a['h']['bins_sha256'], b['h']['bins_sha256'])


if __name__ == '__main__':
    unittest.main()
