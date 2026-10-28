"""PostgreSQL integration tests for pcs.edition_repair: a whole move, a
refusal that writes nothing, rollback after an intermediate failure, and
the scope locks against concurrent edition binding and configuration
changes. The derivation is patched so a test chooses which key an edition
derives; everything else (Dataset.save, ensure_physics_config,
set_lifecycle, the identity triggers) runs for real."""

import itertools
import threading
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.test import TestCase, TransactionTestCase

from pcs.edition_repair import apply_rebind, lock_scope, plan_rebind
from pcs.models import (Dataset, EvgenTag, PhysicsCategory, PhysicsConfig,
                        PhysicsTag, RecoTag, SimuTag)

#: The evgen identity each edition derives, by detector_config; the
#: default when an edition is not listed.
DERIVE = {}

#: Tag numbers unique across the module: the lock tests commit their rows,
#: and the permanence guards forbid deleting them between tests.
NUMBERS = itertools.count(7001)


def fake_key(dataset):
    generator = DERIVE.get(dataset.detector_config, DERIVE.get('*', 'old'))
    evgen = (generator, '', '', '')
    return {'key': (dataset.physics_tag.tag_label, evgen, '', ''),
            'evgen': evgen, 'evgen_source': 'test', 'sample': '',
            'resolved': True}


def sqlstate(error):
    """The PostgreSQL error code anywhere in an exception's chain. A lock
    timeout inside identity_change surfaces as the later 'transaction is
    aborted' error, with the timeout as its context."""
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        code = getattr(error, 'sqlstate', None) or getattr(error, 'pgcode', None)
        if code == '55P03':
            return code
        error = error.__cause__ or error.__context__
    return None


class RepairFixture:
    def make_scope(self):
        DERIVE.clear()
        category, _ = PhysicsCategory.objects.get_or_create(
            digit=1, defaults={'name': 'Test', 'created_by': 'owner'})
        self.p = PhysicsTag.objects.create(tag_number=next(NUMBERS), category=category,
                                           created_by='owner')
        self.e = EvgenTag.objects.create(tag_number=next(NUMBERS), created_by='owner')
        self.s0 = SimuTag.objects.create(tag_number=next(NUMBERS), created_by='owner')
        self.s1 = SimuTag.objects.create(tag_number=next(NUMBERS), created_by='owner')
        self.r = RecoTag.objects.create(tag_number=next(NUMBERS), created_by='owner')
        self.editions = [self.edition('a', self.s0), self.edition('b', self.s1)]
        self.old = self.editions[0].physics_config
        self.assertEqual({ds.physics_config_id for ds in self.editions}, {self.old.pk})
        self.assertEqual(self.old.config_key, self.key('old'))

    def key(self, generator):
        return f'{self.p.tag_label}.{generator}'

    def edition(self, config, simu):
        ds = Dataset(detector_version='26.07.3', detector_config=config,
                     physics_tag=self.p, evgen_tag=self.e, simu_tag=simu,
                     reco_tag=self.r, created_by='owner')
        ds.save()
        return ds

    def bound(self):
        return {ds.pk: Dataset.objects.get(pk=ds.pk).physics_config.config_key
                for ds in self.editions}


@patch('pcs.physics_config.physics_config_key', fake_key)
class ApplyRebindTests(RepairFixture, TestCase):
    def setUp(self):
        with patch('pcs.physics_config.physics_config_key', fake_key):
            self.make_scope()
        DERIVE['*'] = 'new'

    def test_whole_move_rebinds_and_supersedes(self):
        plan, moved = apply_rebind([self.old.label], actor='owner',
                                   reason='corrected derivation', expect_editions=2)
        new = PhysicsConfig.objects.get(config_key=self.key('new'))
        self.assertEqual(moved, {self.old.label: new.label})
        self.assertEqual(set(self.bound().values()), {self.key('new')})
        old = PhysicsConfig.objects.get(pk=self.old.pk)
        self.assertEqual((old.lifecycle, old.superseded_by_id), ('superseded', new.pk))
        self.assertEqual(old.config_key, self.key('old'))  # never re-keyed
        self.assertEqual([e['composed_name'] for e in plan[0]['editions']],
                         [ds.composed_name for ds in self.editions])

    def assert_nothing_written(self):
        self.assertEqual(set(self.bound().values()), {self.key('old')})
        self.assertEqual(PhysicsConfig.objects.get(pk=self.old.pk).lifecycle, 'active')
        self.assertFalse(PhysicsConfig.objects.filter(config_key=self.key('new')).exists())

    def test_refusals_write_nothing(self):
        cases = {
            'count': dict(expect_editions=3),
            'split': dict(expect_editions=2, derive={'b': 'other'}),
            'requestors': dict(expect_editions=2, requestors=['EDT']),
        }
        for name, case in cases.items():
            with self.subTest(name):
                DERIVE.update(case.get('derive', {}))
                if 'requestors' in case:
                    PhysicsConfig.objects.filter(pk=self.old.pk).update(
                        requestors=case['requestors'])
                with self.assertRaises(ValidationError):
                    apply_rebind([self.old.label], actor='owner', reason='r',
                                 expect_editions=case['expect_editions'])
                self.assert_nothing_written()
                DERIVE.clear()
                DERIVE['*'] = 'new'
                PhysicsConfig.objects.filter(pk=self.old.pk).update(requestors=[])

    def test_inactive_existing_target_refused(self):
        DERIVE['*'] = 'old'
        target = self.edition('c', self.s0).physics_config
        self.assertEqual(target.pk, self.old.pk)
        DERIVE['*'] = 'parked'
        parked = self.edition('d', self.s0).physics_config
        from pcs.permanence import set_lifecycle
        set_lifecycle(parked, 'retired', reason='test', changed_by='owner')
        Dataset.objects.filter(detector_config='d').update(physics_config=self.old)
        self.editions = list(Dataset.objects.filter(physics_config=self.old).order_by('pk'))
        with self.assertRaises(ValidationError) as caught:
            apply_rebind([self.old.label], actor='owner', reason='r', expect_editions=4)
        self.assertTrue(any('not active' in m for m in caught.exception.messages))
        self.assertEqual(PhysicsConfig.objects.get(pk=self.old.pk).lifecycle, 'active')

    def test_failure_after_resaves_rolls_everything_back(self):
        with patch('pcs.permanence.set_lifecycle', side_effect=ValidationError('boom')):
            with self.assertRaises(ValidationError):
                apply_rebind([self.old.label], actor='owner', reason='r', expect_editions=2)
        self.assert_nothing_written()

    def test_unknown_label_refused(self):
        with self.assertRaises(ValidationError):
            apply_rebind([self.old.label, 'pc999999'], actor='owner', reason='r',
                         expect_editions=2)
        self.assert_nothing_written()


@patch('pcs.physics_config.physics_config_key', fake_key)
class ScopeLockTests(RepairFixture, TransactionTestCase):
    """While lock_scope holds the scope, another connection can neither bind
    an edition to an old configuration nor change its requestors, metadata
    or lifecycle, nor move one of its editions. Each attempt runs with a
    short lock_timeout and must fail with lock_not_available (55P03).

    The rows must be committed for the other connection to see them, and
    the permanence guards refuse the TRUNCATE a TransactionTestCase flush
    issues, so these tests keep their rows (unique tag numbers) and the
    test database is dropped at the end of the run."""

    def _fixture_teardown(self):
        pass

    def setUp(self):
        with patch('pcs.physics_config.physics_config_key', fake_key):
            self.make_scope()

    def attempt_concurrently(self, action):
        outcome = {}

        def run():
            try:
                with patch('pcs.physics_config.physics_config_key', fake_key), \
                        transaction.atomic():
                    with connection.cursor() as cursor:
                        cursor.execute("SET LOCAL lock_timeout = '500ms'")
                    action()
                outcome['result'] = 'committed'
            except Exception as e:  # report, not hang
                outcome['result'] = sqlstate(e) or repr(e)
            finally:
                connection.close()

        worker = threading.Thread(target=run)
        worker.start()
        worker.join(10)
        return outcome.get('result')

    def assert_blocked_while_locked(self, action):
        with transaction.atomic():
            lock_scope([self.old.label])
            plan_before = plan_rebind([self.old.label])
            self.assertEqual(self.attempt_concurrently(action), '55P03')
            self.assertEqual(plan_rebind([self.old.label]), plan_before)

    def test_new_edition_cannot_bind_to_locked_configuration(self):
        self.assert_blocked_while_locked(lambda: self.edition('late', self.s0))
        self.assertEqual(Dataset.objects.filter(physics_config=self.old).count(), 2)

    def test_existing_edition_cannot_be_bound_by_update(self):
        DERIVE['*'] = 'elsewhere'
        stray = self.edition('stray', self.s0)
        DERIVE.clear()
        self.assert_blocked_while_locked(
            lambda: Dataset.objects.filter(pk=stray.pk).update(physics_config=self.old))

    def test_configuration_associations_and_lifecycle_cannot_change(self):
        from pcs.permanence import set_lifecycle
        changes = {
            'requestors': lambda: PhysicsConfig.objects.filter(pk=self.old.pk)
            .update(requestors=['EDT']),
            'metadata': lambda: PhysicsConfig.objects.filter(pk=self.old.pk)
            .update(metadata={'note': 'x'}),
            'lifecycle': lambda: set_lifecycle(
                PhysicsConfig.objects.get(pk=self.old.pk), 'retired',
                reason='concurrent', changed_by='other'),
        }
        for name, change in changes.items():
            with self.subTest(name):
                self.assert_blocked_while_locked(change)
        old = PhysicsConfig.objects.get(pk=self.old.pk)
        self.assertEqual((old.requestors, old.metadata, old.lifecycle), ([], {}, 'active'))

    def test_locked_edition_cannot_move_away(self):
        self.assert_blocked_while_locked(
            lambda: Dataset.objects.filter(pk=self.editions[0].pk)
            .update(description='touched'))

    def test_unlocked_scope_lets_a_late_edition_in(self):
        # Control: without lock_scope, the same insert commits at once.
        with transaction.atomic():
            plan_rebind([self.old.label])
            self.assertEqual(self.attempt_concurrently(
                lambda: self.edition('late', self.s0)), 'committed')
        self.assertEqual(Dataset.objects.filter(physics_config=self.old).count(), 3)
