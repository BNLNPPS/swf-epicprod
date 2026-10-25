"""One-rerun exclusions retain the work units the person actually reviewed."""
from copy import deepcopy
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from pcs import commands, manifests, residual_exclusions as exclusions, services


class ResidualExclusionTest(TestCase):
    def setUp(self):
        self.rows = [('DIS/example', 'hepmc3.tree.root', 99, n) for n in range(6)]
        self.attempt = SimpleNamespace(jedi_task_id=40857, metadata={
            'manifest': dict(manifests.compact(self.rows), source='submission')})
        self.task = SimpleNamespace(pk=7, overrides={}, name='example', panda_task_id=40857)
        self.choose = patch('pcs.manifests.choose_attempt', return_value=self.attempt).start()
        self.addCleanup(patch.stopall)
        self.query = patch('pcs.services.panda_attempt_bad_input_seqs',
                           return_value=({2, 4}, 'simulation exit-1 candidates')).start()
        self.formatted = manifests.format_rows(self.rows)
        self.preview = exclusions.preview(self.task, self.attempt, self.rows,
                                          {'exact': True}, self.formatted[1:])

    def select(self, **kwargs):
        return exclusions.snapshot(self.task, self.preview['token'], **kwargs)

    def residual(self, selection):
        delivered = ({('DIS', 'example', '0000')}, ['d'],
                     {'d': {'registered': 1, 'unarrived': 0}})
        with patch('pcs.commands._evgen_env', return_value={}):
            return commands._residual_rows(self.task, self.formatted, delivered,
                exclusions.excluded_rows(self.attempt, self.rows, selection))

    def test_real_padded_rows_are_excluded_and_counted(self):
        selection = self.select(bad_input=True)
        residual, coverage = self.residual(selection)
        self.assertEqual([r.rsplit(',', 1)[1] for r in residual], ['0002', '0004', '0005'])
        self.assertEqual(coverage['rows_excluded'], 2)

    def test_no_selection_changes_nothing(self):
        residual, coverage = self.residual(None)
        self.assertEqual(len(residual), 5)
        self.assertEqual(coverage['rows_excluded'], 0)

    def test_manual_integer_and_padded_chunks_are_equivalent(self):
        selection = self.select(listed=[{'file': 'DIS/example', 'chunk': 2},
                                       {'file': 'DIS/example', 'chunk': '0004'}])
        self.assertEqual(selection['seq_numbers'], [3, 5])
        self.assertEqual(self.residual(selection)[1]['rows_excluded'], 2)

    def test_unmatched_or_noninteger_manual_chunks_are_refused(self):
        for chunk in (90, 2.5, True):
            with self.subTest(chunk=chunk), self.assertRaises(services.ServiceError):
                self.select(listed=[{'file': 'DIS/example', 'chunk': chunk}])

    def test_confirmation_does_not_requery_changed_job_state(self):
        self.query.return_value = ({5}, 'changed after preview')
        selection = self.select(bad_input=True)
        self.assertEqual(selection['seq_numbers'], [2, 4])
        self.assertEqual(self.query.call_count, 1)
        self.assertEqual(self.task.overrides, {})

    def test_missing_tampered_or_expired_previews_are_refused(self):
        for token in (None, self.preview['token'] + 'tampered'):
            with self.subTest(token=token), self.assertRaises(services.ServiceError):
                exclusions.snapshot(self.task, token, bad_input=True)
        with patch('django.core.signing.time.time', return_value=0):
            old = exclusions.preview(self.task, self.attempt, self.rows,
                                     {'exact': True}, self.formatted)['token']
        with self.assertRaises(services.ServiceError):
            exclusions.snapshot(self.task, old, bad_input=True)

    def test_preview_is_bound_to_the_task(self):
        self.task.pk = 8
        with self.assertRaises(services.ServiceError):
            self.select(bad_input=True)

    def test_changed_event_boundaries_and_order_are_refused(self):
        selection = self.select(bad_input=True)
        for changed in ([(f, e, 100, c) for f, e, n, c in self.rows], list(reversed(self.rows))):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                exclusions.excluded_rows(self.attempt, changed, selection)
        self.attempt.metadata['manifest'] = dict(
            manifests.compact([(f, e, 100, c) for f, e, n, c in self.rows]), source='submission')
        with self.assertRaises(services.ServiceError):
            self.select(bad_input=True)

    def test_reconstructed_manifest_has_no_exclusion_preview(self):
        result = exclusions.preview(self.task, self.attempt, self.rows,
                                    {'exact': False}, self.formatted)
        self.assertNotIn('token', result)

    def test_all_undelivered_rows_excluded_is_refused(self):
        selection = self.select(listed=[{'file': 'DIS/example', 'chunk': n} for n in range(1, 6)])
        with self.assertRaisesRegex(ValueError, 'zero residual'):
            self.residual(selection)

    def test_worker_builder_reads_attempt_snapshot_not_task_override(self):
        class Reached(Exception):
            pass
        selection = self.select(bad_input=True)
        submitted = SimpleNamespace(metadata={'residual_exclusion': selection})
        self.task.dataset = SimpleNamespace()
        self.task.get_effective_config = lambda: {'data': {}}
        self.task.overrides['residual_exclude'] = {'files': {'unrelated': [0]}}
        with patch('pcs.commands._delivered_row_keys', return_value=(set(), [], {})), \
                patch('pcs.manifests.attempt_manifest', return_value=(self.rows, {'exact': True})), \
                patch('pcs.commands._residual_rows', side_effect=Reached) as builder:
            with self.assertRaises(Reached):
                commands.build_evgen_task_params(self.task, panda_tasks=submitted, residual=True)
        self.assertEqual(builder.call_args.kwargs['exclude'], {2, 4})
        self.assertEqual(self.choose.call_args.args[1], 40857)

    def test_queue_snapshot_is_saved_once_with_fresh_locked_overrides(self):
        selection = self.select(bad_input=True)
        locked = SimpleNamespace(pk=7, overrides={'unrelated': 'kept'}, save=Mock())
        with patch('pcs.services.transaction.atomic'), \
                patch('pcs.services.ProdTask.objects.select_for_update') as lock, \
                patch('pcs.commands.pinned_site', return_value=''), \
                patch('pcs.services.refuse_closed_queue'), \
                patch('pcs.commands.build_evgen_task_params', return_value={'residual': {'rows_residual': 3}}), \
                patch('monitor_app.epicprod_logging.log_epicprod_action'), \
                patch('pcs.residual_exclusions.validate_snapshot'):
            lock.return_value.get.return_value = locked
            mark = services.prodtask_queue_submission(
                task=self.task, kind='rerun_residual', residual_exclusion=selection)
        selection['seq_numbers'].append(6)
        self.assertEqual(mark['residual_exclusion']['seq_numbers'], [2, 4])
        self.assertEqual(locked.overrides['unrelated'], 'kept')
        locked.save.assert_called_once()
        self.assertNotIn('residual_exclude', locked.overrides)

    def test_refused_enqueue_has_no_exclusion_write_or_rollback(self):
        self.task.save = Mock()
        selection = self.select(bad_input=True)
        before = deepcopy(self.task.overrides)
        with patch('pcs.commands.build_evgen_task_params', side_effect=ValueError('refused')):
            with self.assertRaises(services.ServiceError):
                services.prodtask_queue_submission(task=self.task, kind='rerun_residual',
                                                   residual_exclusion=selection)
        self.assertEqual(self.task.overrides, before)
        self.task.save.assert_not_called()

    def test_new_immediate_rerun_does_not_inherit_a_queued_selection(self):
        selection = self.select(bad_input=True)
        self.task.overrides = {'queued': {'residual_exclusion': selection}}
        self.task.status, self.task.save = 'finished', Mock()
        with patch('pcs.services.prodtask_submit_request') as submit, \
                patch('pcs.residual_exclusions.validate_snapshot') as validate:
            services.prodtask_rerun_residual_request(task=self.task, residual_exclusion=None)
        validate.assert_not_called()
        self.assertIsNone(submit.call_args.kwargs['residual_exclusion'])

    def test_front_consumes_the_queued_snapshot(self):
        selection = self.select(bad_input=True)
        self.task.overrides = {'queued': {'residual_exclusion': selection}}
        self.task.status, self.task.save = 'finished', Mock()
        with patch('pcs.services.prodtask_submit_request') as submit, \
                patch('pcs.residual_exclusions.validate_snapshot') as validate:
            services.prodtask_rerun_residual_request(task=self.task)
        validate.assert_called_once_with(self.task, selection)
        self.assertEqual(submit.call_args.kwargs['residual_exclusion'], selection)
        self.assertEqual(submit.call_args.kwargs['residual_of'], 40857)

    def test_submission_copies_snapshot_to_attempt_before_agent_message(self):
        selection = self.select(bad_input=True)
        self.task.panda_task_id, self.task.created_by = None, 'operator'
        submitted = SimpleNamespace(pk=10, task_name='example.try2', metadata={}, save=Mock())
        with patch('pcs.commands.pinned_site', return_value='GREX'), \
                patch('pcs.services.refuse_closed_queue'), \
                patch('pcs.services.prodtask_allocate_panda_tasks', return_value=submitted), \
                patch('monitor_app.activemq_connection.ActiveMQConnectionManager') as bus, \
                patch('pcs.services._clear_queued'):
            services.prodtask_submit_request(task=self.task, residual=True,
                residual_of=40857, residual_exclusion=selection)
        selection['seq_numbers'].append(6)
        self.assertEqual(submitted.metadata['residual_exclusion']['seq_numbers'], [2, 4])
        self.assertEqual(submitted.metadata['residual_of'], 40857)
        submitted.save.assert_called_once()
        bus.return_value.send_message.assert_called_once()

    def api_request(self, data):
        from rest_framework.test import APIRequestFactory, force_authenticate
        from pcs.api_views import ProdTaskViewSet
        request = APIRequestFactory().post('/pcs/api/prod-tasks/example/rerun-residual/', data, format='json')
        force_authenticate(request, user=SimpleNamespace(username='operator', is_authenticated=True))
        self.task.refresh_from_db = Mock()
        with patch.object(ProdTaskViewSet, 'get_object', return_value=self.task), \
                patch.object(ProdTaskViewSet, 'get_serializer', return_value=SimpleNamespace(data={})):
            return ProdTaskViewSet.as_view({'post': 'rerun_residual'})(request, name='example')

    def test_api_requires_the_signed_preview_before_any_queue_write(self):
        with patch('pcs.services.prodtask_queue_submission') as queue:
            response = self.api_request({'exclude_bad_input': True})
        self.assertEqual(response.status_code, 409)
        queue.assert_not_called()

    def test_api_passes_the_frozen_choice_to_enqueue(self):
        with patch('pcs.services.prodtask_queue_submission') as queue:
            response = self.api_request({'exclude_bad_input': True,
                                         'exclusion_preview': self.preview['token']})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(queue.call_args.kwargs['residual_exclusion']['seq_numbers'], [2, 4])
        self.assertEqual(queue.call_args.kwargs['residual_exclusion']['by'], 'operator')

    def test_duplicate_row_content_does_not_expand_the_confirmed_selection(self):
        duplicate = list(self.rows)
        duplicate[2] = duplicate[1]
        selection = {'of': 40857, 'manifest_rows': len(duplicate),
                     'manifest_sha256': exclusions.manifest_identity(duplicate),
                     'seq_numbers': [2]}
        formatted = manifests.format_rows(duplicate)
        with patch('pcs.commands._evgen_env', return_value={}):
            residual, coverage = commands._residual_rows(self.task, formatted,
                (set(), ['d'], {'d': {'registered': 0, 'unarrived': 0}}),
                exclusions.excluded_rows(self.attempt, duplicate, selection))
        self.assertEqual(coverage['rows_excluded'], 1)
        self.assertEqual(residual.count(formatted[1]), 1)
