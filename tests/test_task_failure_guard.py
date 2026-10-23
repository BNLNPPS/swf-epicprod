import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from swf_epicprod import task_failure_guard as guard


class TaskFailureGuardTest(unittest.TestCase):
    def test_floor_and_exact_fraction(self):
        self.assertFalse(guard.should_pause(0, 49))
        self.assertTrue(guard.should_pause(0, 50))
        self.assertFalse(guard.should_pause(3, 47))
        self.assertTrue(guard.should_pause(2, 48))
        self.assertTrue(guard.should_pause(5, 95))
        self.assertFalse(guard.should_pause(6, 94))
        self.assertFalse(guard.should_pause(1000, 100))

    def exercise(self, *, enabled=True, mode='active', dry_run=False,
                 status='running', created=True, read_error=None):
        sample = dict(jeditaskid=123, taskname='group.EIC.test', site='Q',
                      status='running', finished=1, failed=49, since=None)
        ops, queries, log = MagicMock(), MagicMock(), MagicMock()
        ops.queue_task_operation.return_value = (SimpleNamespace(id='request'), created)
        queries.get_task_operation_targets.return_value = [dict(sample, status=status)]
        with patch.dict(sys.modules, {
            'monitor_app.panda.operations': ops,
            'monitor_app.panda.queries': queries,
            'monitor_app.epicprod_logging': log,
        }), patch.object(guard, 'observations', return_value=[sample], side_effect=read_error) as read:
            result = guard.run(['Q'], enabled=enabled, mode=mode,
                               dry_run=dry_run, created_by='front')
        return result, ops, log, read

    def test_disabled_does_not_read_or_act(self):
        result, ops, log, read = self.exercise(enabled=False)
        self.assertEqual(result, [])
        read.assert_not_called()
        ops.queue_task_operation.assert_not_called()

    def test_shadow_and_dry_run_never_enqueue(self):
        for args in ({'mode': 'shadow'}, {'dry_run': True}):
            result, ops, log, _ = self.exercise(**args)
            self.assertEqual(result[0]['outcome'], 'would_pause')
            ops.queue_task_operation.assert_not_called()
            log.log_epicprod_action.assert_not_called()

    def test_only_pause_with_durable_evidence(self):
        result, ops, log, _ = self.exercise()
        call = ops.queue_task_operation.call_args.kwargs
        self.assertEqual(call['operation'], 'pause')
        self.assertEqual(call['source'], guard.SOURCE)
        self.assertEqual(call['evidence']['failed'], 49)
        self.assertEqual(result[0]['operation_id'], 'request')
        log.log_epicprod_action.assert_called_once()

    def test_existing_pending_pause_does_not_repeat_notice(self):
        result, ops, log, _ = self.exercise(created=False)
        self.assertEqual(result[0]['outcome'], 'pause_pending')
        log.log_epicprod_action.assert_not_called()

    def test_operator_state_change_wins(self):
        for status in ('paused', 'done', 'aborted'):
            result, ops, log, _ = self.exercise(status=status)
            self.assertEqual(result[0]['outcome'], 'state_changed')
            ops.queue_task_operation.assert_not_called()

    def test_read_failure_is_not_a_zero_count(self):
        with self.assertRaisesRegex(RuntimeError, 'DB down'):
            self.exercise(read_error=RuntimeError('DB down'))
