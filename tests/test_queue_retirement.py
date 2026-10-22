"""A retired destination cannot revive through a restored config or probe."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from django.test import override_settings
from swf_epicprod import front
from swf_epicprod.regulation import RETIRED_QUEUES, require_task_destination

OLD = 'BNL_OSG_EPIC_PROD_1'
ACTIVE = 'BNL_OSG_PanDA_1'


class RetirementTests(unittest.TestCase):
    def test_restored_and_malformed_front_settings_cannot_reopen(self):
        with patch.object(front, 'setting', return_value=[OLD, ACTIVE, 'UM_GREX_PanDA_1']):
            self.assertEqual(front.regulated_queues(), [ACTIVE, 'UM_GREX_PanDA_1'])
        with patch.object(front, 'setting', return_value='bad config'):
            self.assertNotIn(OLD, front.regulated_queues())
            self.assertIn(OLD, front.closed_queues())
        with patch.object(front, 'setting', return_value={'Q': 'operator closed'}):
            self.assertEqual(front.closed_queues()['Q'], 'operator closed')
            self.assertIn(OLD, front.closed_queues())
        with self.assertRaisesRegex(ValueError, 'existing tasks drain in place'):
            require_task_destination(OLD)
        require_task_destination(ACTIVE)
        require_task_destination('UM_GREX_PanDA_1')

    @override_settings(CANARY_CLOSED_QUEUES=RETIRED_QUEUES)
    def test_old_probe_config_and_manual_payload_probe_cannot_submit(self):
        from canary.probe import dispatch_payload_canary, probe_config
        q = SimpleNamespace(name=OLD, data={'probe': {'enabled': True}})
        self.assertFalse(probe_config(q)['enabled'])
        q.name = ACTIVE
        self.assertTrue(probe_config(q)['enabled'])
        # Refused before database access or a submission subprocess.
        result = dispatch_payload_canary(None, 'example', OLD)
        self.assertEqual(result['outcome'], 'closed')
