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


def attempts(pattern, hosts=('h1', 'h2', 'h3')):
    """Newest first: F failed, S succeeded, W a worker loss (failed, set aside)."""
    return [{'failed': c in 'FW', 'worker_loss': c == 'W', 'host': hosts[i % len(hosts)]}
            for i, c in enumerate(pattern)]


class RollingRuleTest(unittest.TestCase):
    """The rule mailed to the collaboration (2026-10-23)."""

    def test_ten_consecutive_failures_trip(self):
        trip, ev = guard.judge_rolling(attempts('F' * 10 + 'S' * 40))
        self.assertEqual((trip, ev['trigger'], ev['consecutive_failed']), (True, 'consecutive', 10))
        self.assertFalse(guard.judge_rolling(attempts('F' * 9 + 'S' * 41))[0])

    def test_twenty_five_of_fifty_trip(self):
        trip, ev = guard.judge_rolling(attempts('FS' * 25))
        self.assertEqual((trip, ev['trigger'], ev['window_failed']), (True, 'window', 25))
        self.assertFalse(guard.judge_rolling(attempts('S' + 'FS' * 24 + 'S'))[0])
        # Fewer than fifty completed attempts never trip the window.
        self.assertFalse(guard.judge_rolling(attempts('FFS' * 15))[0])

    def test_worker_losses_count_as_neither(self):
        # Twelve losses between the failures: still nine counted, no trip.
        self.assertFalse(guard.judge_rolling(attempts('FWFWFWFWFWFWFWFWFWWWW' + 'S' * 40))[0])
        trip, ev = guard.judge_rolling(attempts('W' * 30 + 'F' * 10 + 'S' * 40))
        self.assertEqual((trip, ev['set_aside']), (True, 30))
        # A batch of worker losses alone never pauses.
        self.assertFalse(guard.judge_rolling(attempts('W' * 60 + 'S' * 50))[0])

    def test_failures_on_fewer_than_three_hosts_are_the_node_guards(self):
        self.assertFalse(guard.judge_rolling(attempts('F' * 20 + 'S' * 30, hosts=('h1', 'h2')))[0])
        self.assertTrue(guard.judge_rolling(attempts('F' * 20 + 'S' * 30))[0])

    def test_thresholds_from_settings(self):
        cfg = guard.rolling_settings(lambda key, default: {'front.task_guard.run_failed': 5}.get(key, default))
        self.assertEqual(cfg['run_failed'], 5)
        self.assertTrue(guard.judge_rolling(attempts('F' * 5 + 'S' * 45), cfg)[0])

    def test_attempt_rows_read_panda_fields(self):
        rows = [('failed', 100, 0, 'slot1_2@comphy.fsu.edu'), ('failed', 0, 300, 'a@x'),
                ('failed', 0, 0, 'b@y'), ('finished', 0, 0, 'c@z')]
        self.assertEqual([(a['failed'], a['worker_loss'], a['host']) for a in guard.attempt_rows(rows)],
                         [(True, True, 'comphy.fsu.edu'), (True, True, 'x'),
                          (True, False, 'y'), (False, False, 'z')])

    def exercise(self, pattern, *, rolling_mode, mode='active', recorded=False):
        sample = dict(jeditaskid=7, taskname='group.EIC.t', site='Q', status='running',
                      finished=40, failed=10, since=None, attempts=attempts(pattern))
        ops, queries, log = MagicMock(), MagicMock(), MagicMock()
        ops.queue_task_operation.return_value = (SimpleNamespace(id='req'), True)
        queries.get_task_operation_targets.return_value = [dict(sample, status='running')]
        with patch.dict(sys.modules, {
            'monitor_app.panda.operations': ops, 'monitor_app.panda.queries': queries,
            'monitor_app.epicprod_logging': log,
        }), patch.object(guard, 'observations', return_value=[sample]), \
                patch.object(guard, '_shadow_recorded', return_value=recorded):
            result = guard.run(['Q'], enabled=True, mode=mode, dry_run=False,
                               created_by='front', rolling_mode=rolling_mode)
        return result, ops, log

    def test_shadow_records_once_and_never_pauses(self):
        result, ops, log = self.exercise('F' * 10 + 'S' * 40, rolling_mode='shadow')
        self.assertEqual((result[0]['rule'], result[0]['outcome']), ('rolling', 'would_pause'))
        ops.queue_task_operation.assert_not_called()
        self.assertEqual(log.log_epicprod_action.call_args.args[1], 'task_failure_guard_shadow')
        _, _, log = self.exercise('F' * 10 + 'S' * 40, rolling_mode='shadow', recorded=True)
        log.log_epicprod_action.assert_not_called()

    def test_active_pauses_with_its_own_source(self):
        result, ops, log = self.exercise('F' * 10 + 'S' * 40, rolling_mode='active')
        self.assertEqual(ops.queue_task_operation.call_args.kwargs['source'], guard.ROLLING_SOURCE)
        self.assertEqual(result[0]['outcome'], 'pause_requested')

    def test_off_and_front_shadow_never_pause(self):
        for kwargs in ({'rolling_mode': 'off'}, {'rolling_mode': 'active', 'mode': 'shadow'}):
            _, ops, _ = self.exercise('F' * 10 + 'S' * 40, **kwargs)
            ops.queue_task_operation.assert_not_called()
