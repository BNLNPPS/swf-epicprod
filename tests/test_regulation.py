import copy
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import unittest

from swf_epicprod.regulation import throttler_verdict
from swf_epicprod.front import decide, hours_summary


class RegulationTests(unittest.TestCase):
    def test_switch_requires_fresh_runtime_evidence_for_both_vos(self):
        now = datetime.now(timezone.utc)
        report = {'age_seconds': 5, 'record': {'jedi_regulation': {'voices': {
            vo: {'at': now.isoformat()} for vo in ('wlcg', 'epic')}}}}
        modes = dict(wlcg='throttle', epic='throttle')
        self.assertFalse(throttler_verdict(report, modes, now=now)['red'])
        self.assertTrue(throttler_verdict(report, dict(modes, epic='observe'), now=now)['red'])
        report['record']['jedi_regulation']['voices']['epic']['at'] = (now - timedelta(minutes=11)).isoformat()
        self.assertTrue(throttler_verdict(report, modes, now=now)['red'])

    def test_large_head_does_not_block_fitting_task(self):
        from test_front_decide import census, entry, run
        result = run(census(), [entry('large', rows=20000), entry('small', rows=100)])
        self.assertEqual([r[0] for r in result], ['oversize', 'would_feed'])

    def test_shared_capacity_is_counted_once(self):
        result = hours_summary({}, {q: {'ceiling': 1000, 'capacity_pool': 'osg'} for q in ('A', 'B')})
        self.assertEqual(result['capacity']['total'], 1000)


class NoticeTests(unittest.TestCase):
    def test_decline_with_backlog_notifies_without_zero_running(self):
        spec = importlib.util.spec_from_file_location('notice', Path(__file__).parents[1] / 'scripts/prod-notify.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        now = datetime.now(timezone.utc).timestamp()
        state = {'regulation': {'Q': {'history': [[now - 1200, 1000]], 'falling_since': now - 1000}}}
        snapshot = {'sites': [{'queues': [{'id': 'Q', 'running': 433, 'queued': 5000}]}]}
        notices = module.regulation_triggers(snapshot, state)
        self.assertEqual(notices[0][0], 'regulation.degraded')
        self.assertEqual(module.regulation_triggers(snapshot, state), [])
        # Aging the original high sample out must not announce recovery.
        state['regulation']['Q']['history'][0][0] = now - 4000
        self.assertEqual(module.regulation_triggers(snapshot, state), [])
        self.assertEqual(state['regulation']['Q']['falling_peak'], 1000)
        snapshot['sites'][0]['queues'][0]['running'] = 1000
        self.assertEqual(module.regulation_triggers(snapshot, state)[0][0], 'regulation.recovered')
        self.assertNotIn('falling_peak', state['regulation']['Q'])
