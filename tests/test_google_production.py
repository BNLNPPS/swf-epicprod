"""Seed reservations and persistent supply boundaries; no external writes."""
import unittest
from types import SimpleNamespace as Obj
from unittest.mock import MagicMock, patch

from swf_epicprod import google_production as prod
from swf_epicprod.production_es import gun_seed


class SeedReservationsTest(unittest.TestCase):
    def test_intervals_equal_payload_seeds(self):
        for sample in ('gES1', 'pES3', 'gOP000001'):
            stem = 'neutron_5GeV_3to50deg.' + sample
            intervals = prod.seed_intervals(stem, 1000)
            actual = {i for low, high in intervals for i in range(low, high + 1)}
            self.assertEqual(actual, {gun_seed(stem, i) for i in range(1000)})

    def test_adjacent_reservations_do_not_overlap(self):
        self.assertFalse(prod.overlaps([(1, 10)], [(11, 20)]))
        self.assertTrue(prod.overlaps([(1, 10)], [(10, 20)]))

    def test_wrap_reserves_zero_mapping(self):
        with patch.object(prod, 'MODULUS', 11), patch.object(prod.hashlib, 'sha256') as digest:
            digest.return_value.hexdigest.return_value = '0000001'
            spans = prod.seed_intervals('wrap', 11)
            self.assertTrue(prod.overlaps(spans, [(1, 1)]))
            self.assertTrue(prod.overlaps(spans, [(10, 10)]))
            self.assertLess(sum(b - a + 1 for a, b in spans), 11)


class InventoryTest(unittest.TestCase):
    def inventory(self, state=None, *, attempt=True, taskid=123):
        task = MagicMock(pk=9, panda_task_id=taskid)
        task.panda_tasks.order_by.return_value.first.return_value = (
            Obj(jedi_task_id=taskid) if attempt else None)
        with patch('pcs.models.ProdTask.objects') as tasks, patch('django.db.connections') as db:
            tasks.filter.return_value.order_by.return_value = [task]
            cursor = db.__getitem__.return_value.cursor.return_value.__enter__.return_value
            cursor.fetchone.return_value = (state,) if state else None
            return prod.task_inventory(), task

    def test_running_counts_as_committed(self):
        self.assertEqual(self.inventory('running')[0], ([123], None))

    def test_completed_releases_supply_slot(self):
        self.assertEqual(self.inventory('done')[0], ([], None))

    def test_failed_paused_incomplete_or_unknown_hold_supply(self):
        for state in ('finished', 'failed', 'broken', 'aborted', 'exhausted', 'paused', None):
            with self.subTest(state=state), self.assertRaises(RuntimeError):
                self.inventory(state)

    def test_submitted_without_jedi_id_still_reserves_slot(self):
        self.assertEqual(self.inventory(taskid=None)[0], ([9], None))

    def test_draft_is_reused_after_staging_failure(self):
        (active, draft), task = self.inventory(attempt=False, taskid=None)
        self.assertEqual(active, [])
        self.assertIs(draft, task)


if __name__ == '__main__':
    unittest.main()
