"""Manifest chunks past 9999: the row reader (pcs.manifests), the RECO row
key (pcs.commands.reco_row_key) and the delivered-file reader
(swf_epicprod.analytics.file_events.source_of) read five-digit chunks
alike, and a manifest that did not parse whole is never taken as exact.
Task 41367 (p2490): 2 inputs x 50,000 chunks, recorded as 20,000 rows."""
import datetime as dt
import io
import json
import tarfile
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest import mock

from pcs import manifests
from pcs import manifest_repair
from pcs.commands import _residual_rows, reco_row_key
from swf_epicprod.analytics.delivery_daily import count_each_row_once
from swf_epicprod.analytics.file_events import source_of

EVGEN = 'root://dtn-eic.jlab.org//volatile/eic/EPIC/EVGEN/EXCLUSIVE/DDIS/run001'
RECO = '/RECO/26.07.3/epic_craterlake/EXCLUSIVE/DDIS'
TRY2 = '/RECO/26.07.3/epic_craterlake/try2/EXCLUSIVE/DDIS'


def rows_of(nchunks, file=EVGEN, nevents=100):
    return [(file, 'hepmc3.tree.root', nevents, c) for c in range(nchunks)]


def reco(location, chunk, mark=''):
    return f'{location}/run001.{chunk:04d}{mark}.eicrecon.edm4eic.root'


class Rows(unittest.TestCase):

    def test_chunks_across_the_four_digit_boundary(self):
        text = '\n'.join(manifests.format_rows(
            [(EVGEN, 'hepmc3.tree.root', 100, c) for c in (0, 9999, 10000, 49999)]))
        rows, rejected = manifests.parse_manifest(text)
        self.assertEqual([c for *_x, c in rows], [0, 9999, 10000, 49999])
        self.assertEqual(rejected, [])

    def test_format_pads_to_four_and_no_further(self):
        self.assertEqual([r.rsplit(',', 1)[1] for r in manifests.format_rows(
            [(EVGEN, 'x', 1, c) for c in (7, 9999, 12345)])],
            ['0007', '9999', '12345'])

    def test_50k_chunks_round_trip(self):
        rows = rows_of(50000)
        record = manifests.compact(rows)
        self.assertEqual(record['rows'], 50000)
        self.assertEqual(record['files'][0]['nchunks'], 50000)
        self.assertEqual(manifests.expand(record), rows)
        self.assertEqual(manifests.parse_rows('\n'.join(manifests.format_rows(rows))), rows)

    def test_rejected_lines_are_returned(self):
        rows, rejected = manifests.parse_manifest(
            f'{EVGEN},hepmc3.tree.root,100,0001\n\nnot,a,row\n')
        self.assertEqual((len(rows), rejected), (1, ['not,a,row']))

    def test_strict_reader_never_returns_a_partial_manifest(self):
        with self.assertRaises(manifests.ManifestUnavailable):
            manifests.parse_rows(f'{EVGEN},hepmc3.tree.root,100,0001\nnot,a,row\n')


class RecoKeys(unittest.TestCase):

    def test_five_digit_outputs_key_and_match_their_rows(self):
        for chunk in (9999, 10000, 49999):
            key = reco_row_key(reco(RECO, chunk))
            self.assertEqual(key[-1], f'{chunk:04d}')
            self.assertEqual(reco_row_key(reco(RECO, chunk, '.p4614093')), key)
            self.assertEqual(reco_row_key(reco(TRY2, chunk)), key)

    def test_copies_past_9999_are_counted_once(self):
        t = dt.datetime(2026, 10, 27, tzinfo=dt.timezone.utc)
        first = (reco(RECO, 12345), '26.07', t, 1)
        diverted = (reco(TRY2, 12345, '.p4614093'), '26.07', t + dt.timedelta(hours=1), 1)
        kept, dup = count_each_row_once([first, diverted])
        self.assertEqual(([r[0] for r in kept], dup), ([first[0]], {TRY2: 1}))

    def test_source_of_reads_five_digits(self):
        self.assertEqual(source_of(reco(RECO, 12345)), ('run001', 12345))
        self.assertEqual(source_of(reco(RECO, 7)), ('run001', 7))

    def test_residual_leaves_out_delivered_rows_past_9999(self):
        # Rows name the input below EVGEN, as _chunk_rows writes them;
        # keys are what _delivered_row_keys gives for each RECO output.
        csv_rows = manifests.format_rows(rows_of(10002, 'EXCLUSIVE/DDIS/run001'))
        keys = {reco_row_key(reco(RECO, c))[2:] for c in range(10001)}
        with mock.patch('pcs.commands._evgen_env', return_value={}):
            residual, cov = _residual_rows(None, csv_rows, (keys, [], {}))
        self.assertEqual(residual, [csv_rows[-1]])
        self.assertEqual(cov['rows_residual'], 1)


class Truncation(unittest.TestCase):

    attempt = SimpleNamespace(jedi_task_id=41367, metadata={})

    def short_record(self):
        rows = rows_of(10000) + rows_of(10000, EVGEN.replace('run001', 'run002'))
        return manifests.compact(rows)

    def test_record_short_of_the_submitted_rows_is_not_exact(self):
        with mock.patch.object(manifests, 'attempt_row_count', return_value=100000):
            why = manifests.truncation(self.attempt, self.short_record())
        self.assertIn('20000 rows', why)
        self.assertIn('100000', why)

    def test_genuine_10k_chunk_file_stands(self):
        with mock.patch.object(manifests, 'attempt_row_count', return_value=20000):
            self.assertIsNone(manifests.truncation(self.attempt, self.short_record()))

    def test_record_below_the_boundary_is_never_questioned(self):
        with mock.patch.object(manifests, 'attempt_row_count') as count:
            self.assertIsNone(manifests.truncation(self.attempt, manifests.compact(rows_of(500))))
        count.assert_not_called()

    def test_unknown_row_count_does_not_validate_a_boundary_record(self):
        with mock.patch.object(manifests, 'attempt_row_count', return_value=None):
            self.assertIn('cannot be checked', manifests.truncation(self.attempt, self.short_record()))

    def test_short_record_blocks_preview_without_rewriting_it(self):
        attempt = SimpleNamespace(pk=1, try_number=1, jedi_task_id=41367, task_name='t',
                                  metadata={'manifest': self.short_record()})
        before = json.dumps(attempt.metadata, sort_keys=True)
        with mock.patch.object(manifests, 'attempt_row_count', return_value=100000), \
                mock.patch.object(manifests, 'fetch_from_cache') as fetch, \
                mock.patch.object(manifests, 'store') as store:
            with self.assertRaisesRegex(manifests.ManifestUnavailable, 'needs repair'):
                manifests.attempt_manifest(SimpleNamespace(), attempt)
        fetch.assert_not_called()
        store.assert_not_called()
        self.assertEqual(json.dumps(attempt.metadata, sort_keys=True), before)


class AttemptUnits(unittest.TestCase):

    def params(self, **overrides):
        return dict({'noInput': True, 'nEvents': 100000, 'nEventsPerJob': 1,
                     'jobParameters': [{'param_type': 'pseudo_input', 'value': '${SEQNUMBER}'}]},
                    **overrides)

    def test_serial_dispatcher_counts_rows(self):
        with mock.patch.object(manifests, '_taskparams', return_value=json.dumps(self.params())):
            self.assertEqual(manifests.attempt_row_count(41367), 100000)

    def test_other_event_units_are_not_row_counts(self):
        for changed in ({'noInput': False}, {'nEventsPerJob': 100},
                        {'jobParameters': []}, {'nEvents': '100000'}):
            with self.subTest(changed=changed), mock.patch.object(
                    manifests, '_taskparams', return_value=json.dumps(self.params(**changed))):
                self.assertIsNone(manifests.attempt_row_count(41367))

    def test_missing_parameters_are_not_a_count(self):
        with mock.patch.object(manifests, '_taskparams', return_value=''):
            self.assertIsNone(manifests.attempt_row_count(41367))

    def test_invalid_parameters_raise(self):
        with mock.patch.object(manifests, '_taskparams', return_value='not JSON'):
            with self.assertRaises(manifests.ManifestUnavailable):
                manifests.attempt_row_count(41367)

    def test_unreadable_attempt_does_not_hide_a_usable_sibling(self):
        bad = SimpleNamespace(jedi_task_id=1, try_number=2, metadata={})
        good = SimpleNamespace(jedi_task_id=2, try_number=1, metadata={})
        task = SimpleNamespace(panda_tasks=SimpleNamespace(all=lambda: [bad, good]))
        with mock.patch.object(manifests, 'attempt_row_count', side_effect=[
                manifests.ManifestUnavailable('bad taskparams'), 7230]), \
                self.assertLogs('pcs.manifests', level='WARNING'):
            self.assertIs(manifests.choose_attempt(task), good)


class Sandbox(unittest.TestCase):

    def archive(self, files):
        body = io.BytesIO()
        with tarfile.open(fileobj=body, mode='w:gz') as archive:
            for name, text in files.items():
                data = text.encode()
                member = tarfile.TarInfo(name)
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
        return body.getvalue()

    def test_larger_rejected_csv_cannot_be_hidden_by_a_smaller_valid_one(self):
        good = f'{EVGEN},hepmc3.tree.root,100,0001\n'
        response = SimpleNamespace(status_code=200, content=self.archive(
            {'small.csv': good, 'actual.csv': good + 'bad,line\nbad,line\n'}))
        with mock.patch.object(manifests, 'sandbox_ref', return_value=('https://cache.test', 'a.tgz')), \
                mock.patch('requests.get', return_value=response):
            with self.assertRaisesRegex(manifests.ManifestUnavailable, 'not the whole attempt'):
                manifests.fetch_from_cache(41367)


    def test_valid_five_digit_sandbox_rows_are_complete(self):
        rows = [(EVGEN, 'hepmc3.tree.root', 100, c) for c in (9999, 10000, 49999)]
        response = SimpleNamespace(status_code=200, content=self.archive(
            {'actual.csv': '\n'.join(manifests.format_rows(rows))}))
        with mock.patch.object(manifests, 'sandbox_ref', return_value=('https://cache.test', 'a.tgz')), \
                mock.patch('requests.get', return_value=response):
            self.assertEqual(manifests.fetch_from_cache(41367), rows)

    def test_equal_size_csvs_cannot_hide_rejected_rows(self):
        first = '\n'.join(manifests.format_rows(rows_of(2)))
        other = manifests.format_rows(rows_of(1))[0] + '\nbad,line'
        response = SimpleNamespace(status_code=200, content=self.archive(
            {'first.csv': first, 'actual.csv': other}))
        with mock.patch.object(manifests, 'sandbox_ref', return_value=('https://cache.test', 'a.tgz')), \
                mock.patch('requests.get', return_value=response):
            with self.assertRaisesRegex(manifests.ManifestUnavailable, 'ambiguous'):
                manifests.fetch_from_cache(41367)


class ExplicitRepair(unittest.TestCase):

    def setUp(self):
        self.rows = [(EVGEN, 'hepmc3.tree.root', 100, c) for c in (9999, 10000, 49999)]
        self.old = manifests.make_record(self.rows[:1], 'submission')
        self.attempt = SimpleNamespace(jedi_task_id=41367, task_name='composed.task',
                                       metadata={'manifest': self.old, 'other': {'value': 1}})

    def prepare(self, count=3):
        with mock.patch.object(manifests, 'fetch_from_cache', return_value=self.rows), \
                mock.patch.object(manifests, 'attempt_row_count', return_value=count):
            return manifest_repair.prepare(self.attempt)

    def replacement(self, prepared, metadata=None, **overrides):
        args = {'rows': 3, 'events': 300, 'sha256': manifests.rows_sha256(self.rows),
                'actor': 'operator', 'reason': 'verify exact submitted CSV'}
        args.update(overrides)
        return manifest_repair.replacement(
            self.attempt.metadata if metadata is None else metadata, prepared, **args)

    def test_dry_run_reads_exact_csv_without_changing_the_attempt(self):
        before = deepcopy(self.attempt.metadata)
        plan = self.prepare()
        self.assertEqual((plan['summary']['rows'], plan['summary']['events']), (3, 300))
        self.assertEqual(plan['summary']['sha256'], manifests.rows_sha256(self.rows))
        self.assertEqual(self.attempt.metadata, before)

    def test_known_submitted_count_must_agree(self):
        with self.assertRaisesRegex(manifests.ManifestUnavailable, 'attempt declares'):
            self.prepare(count=4)

    def test_exact_csv_can_repair_a_record_with_unknown_count_units(self):
        plan = self.prepare(count=None)
        self.assertEqual(plan['summary']['count_basis'], 'exact sandbox CSV alone')
        self.assertEqual(self.replacement(plan)['manifest']['rows'], 3)

    def test_verified_boundary_record_stays_usable_without_submitted_count(self):
        self.rows = rows_of(10000)
        plan = self.prepare(count=None)
        fixed = self.replacement(plan, rows=10000, events=1000000,
                                 sha256=manifests.rows_sha256(self.rows))
        with mock.patch.object(manifests, 'attempt_row_count') as count:
            self.assertIsNone(manifests.truncation(self.attempt, fixed['manifest']))
        count.assert_not_called()

    def test_verification_does_not_cover_changed_boundary_rows(self):
        self.rows = rows_of(10000)
        plan = self.prepare(count=None)
        fixed = self.replacement(plan, rows=10000, events=1000000,
                                 sha256=manifests.rows_sha256(self.rows))
        fixed['manifest']['files'][0]['nevents'] = 101
        with mock.patch.object(manifests, 'attempt_row_count', return_value=None):
            self.assertIn('cannot be checked', manifests.truncation(self.attempt, fixed['manifest']))

    def test_replacement_keeps_prior_record_and_concurrent_unrelated_metadata(self):
        plan = self.prepare()
        current = deepcopy(self.attempt.metadata)
        current['other']['value'] = 2
        fixed = self.replacement(plan, current)
        self.assertEqual(fixed['other']['value'], 2)
        self.assertEqual(fixed['manifest']['source'], 'cache')
        self.assertEqual(fixed['manifest']['rows'], 3)
        previous = fixed['manifest_superseded'][-1]
        self.assertEqual(previous['manifest'], self.old)
        self.assertEqual(previous['by'], 'operator')
        self.assertIn('reason', previous)
        self.assertEqual(self.attempt.metadata['manifest'], self.old)

    def test_changed_manifest_is_not_overwritten(self):
        plan = self.prepare()
        current = deepcopy(self.attempt.metadata)
        current['manifest']['rows'] = 2
        with self.assertRaisesRegex(ValueError, 'record changed'):
            self.replacement(plan, current)

    def test_all_reviewed_values_and_a_reason_are_required(self):
        plan = self.prepare()
        for invalid in ({'rows': 4}, {'events': 400}, {'sha256': '0' * 64},
                        {'actor': ''}, {'reason': ''}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.replacement(plan, **invalid)

if __name__ == '__main__':
    unittest.main()

