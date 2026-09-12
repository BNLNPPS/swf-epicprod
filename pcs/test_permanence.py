"""PostgreSQL integration tests: exercise bypasses, not just model helpers."""

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection, DatabaseError, transaction
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIRequestFactory, force_authenticate

from pcs.api_views import EvgenTagViewSet
from pcs.models import (BackgroundTag, Dataset, EvgenTag, IdentityHistory,
                        PhysicsCategory, PhysicsConfig, PhysicsTag, RecoTag, SimuTag)
from pcs.permanence import identity_change, set_lifecycle


class PermanentIdentityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        category = PhysicsCategory.objects.create(digit=1, name='Test', created_by='owner')
        cls.p = PhysicsTag.objects.create(tag_number=1001, category=category, created_by='owner')
        cls.e = EvgenTag.objects.create(tag_number=1, created_by='owner', parameters={'generator': 'test'})
        cls.s = SimuTag.objects.create(tag_number=1, created_by='owner')
        cls.r = RecoTag.objects.create(tag_number=1, created_by='owner')
        cls.k = BackgroundTag.objects.create(tag_number=1, created_by='owner')
        cls.pc = PhysicsConfig.objects.create(label='pc1', config_key='test-key', physics_tag=cls.p,
                                               requestors=['EDT'], metadata={'requestors': {'history': ['original']}})
        cls.user = get_user_model().objects.create_user(username='owner')

    @property
    def identities(self):
        return (self.p, self.e, self.s, self.r, self.k, self.pc)

    def assert_sql_rejected(self, query, params=None):
        with self.assertRaises(DatabaseError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(query, params)

    def test_all_six_tables_reject_orm_bulk_and_sql_erasure(self):
        for instance in self.identities:
            with self.subTest(table=instance._meta.db_table):
                with self.assertRaises(ValidationError):
                    instance.delete()
                with self.assertRaises(ValidationError):
                    type(instance).objects.all().delete()
                table = connection.ops.quote_name(instance._meta.db_table)
                self.assert_sql_rejected(f'DELETE FROM {table} WHERE id=%s', [instance.pk])
                self.assert_sql_rejected(f'TRUNCATE {table} CASCADE')
                self.assertTrue(type(instance).objects.filter(pk=instance.pk).exists())
        self.assert_sql_rejected('TRUNCATE pcs_dataset CASCADE')

    def test_identity_fields_and_numbers_cannot_be_reassigned(self):
        for instance in self.identities:
            table = connection.ops.quote_name(instance._meta.db_table)
            self.assert_sql_rejected(f'UPDATE {table} SET id=id+100')
            self.assert_sql_rejected(f"UPDATE {table} SET created_by='rewritten'")
        self.assert_sql_rejected('UPDATE pcs_evgen_tag SET tag_number=99')
        self.assert_sql_rejected("UPDATE pcs_evgen_tag SET tag_label='e99'")
        self.assert_sql_rejected("UPDATE pcs_physics_config SET label='pc99'")
        self.assert_sql_rejected("UPDATE pcs_physics_config SET config_key='rewritten'")
        self.assert_sql_rejected("UPDATE pcs_physics_config SET sample_name='rewritten'")

    def test_replica_mode_does_not_disable_guards(self):
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL session_replication_role='replica'")
        self.assert_sql_rejected('DELETE FROM pcs_evgen_tag')
        self.assert_sql_rejected("UPDATE pcs_evgen_tag SET tag_number=99")
        self.assert_sql_rejected('TRUNCATE pcs_evgen_tag CASCADE')
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL session_replication_role='origin'")

    def test_corrections_and_requestor_history_are_retained(self):
        with identity_change('curator', 'Fix attribution'):
            PhysicsConfig.objects.filter(pk=self.pc.pk).update(requestors=['INCLUSIVE'], metadata={})
        event = IdentityHistory.objects.filter(table_name='pcs_physics_config', record_id=self.pc.pk).last()
        self.assertEqual(event.before['requestors'], ['EDT'])
        self.assertEqual(event.before['metadata']['requestors']['history'], ['original'])
        self.assertEqual(event.after['requestors'], ['INCLUSIVE'])
        self.assertEqual((event.actor, event.reason), ('curator', 'Fix attribution'))
        self.assert_sql_rejected("UPDATE pcs_identity_history SET actor='forged'")
        self.assert_sql_rejected('DELETE FROM pcs_identity_history')
        self.assert_sql_rejected('TRUNCATE pcs_identity_history')

    def test_draft_correction_is_recorded_locked_physics_cannot_change(self):
        EvgenTag.objects.filter(pk=self.e.pk).update(parameters={'generator': 'corrected'})
        event = IdentityHistory.objects.filter(table_name='pcs_evgen_tag', record_id=self.e.pk).last()
        self.assertEqual(event.before['parameters'], {'generator': 'test'})
        self.e.status = 'locked'
        self.e.save(update_fields=['status'])
        self.assert_sql_rejected("UPDATE pcs_evgen_tag SET parameters='{}'")
        self.assert_sql_rejected("UPDATE pcs_evgen_tag SET status='draft'")
        # Disposition is orthogonal to the one-way lock.
        retired = set_lifecycle(self.e, 'retired', reason='Superseded technique', changed_by='owner')
        self.assertEqual(retired.status, 'locked')

    def test_retirement_reactivation_supersession_and_old_lookup(self):
        replacement = EvgenTag.objects.create(tag_number=2, created_by='owner')
        with self.assertRaises(ValidationError):
            set_lifecycle(self.e, 'retired', reason='', changed_by='owner')
        set_lifecycle(self.e, 'superseded', reason='Corrected generator', changed_by='owner', replacement=replacement)
        self.assertEqual(EvgenTag.objects.get(tag_label='e1').superseded_by, replacement)
        self.assertFalse(EvgenTag.objects.active().filter(pk=self.e.pk).exists())
        set_lifecycle(self.e, 'active', reason='Review reversal', changed_by='owner')
        self.assertTrue(EvgenTag.objects.active().filter(pk=self.e.pk).exists())
        self.assert_sql_rejected("UPDATE pcs_evgen_tag SET lifecycle='nonsense'")
        self.assert_sql_rejected("UPDATE pcs_evgen_tag SET lifecycle='superseded', superseded_by_id=id")

    def test_edition_fold_preserves_old_bindings_and_configuration(self):
        # Bulk create intentionally exercises a path bypassing Dataset.save.
        ds = Dataset(dataset_name='old-name', composed_name='old-name', did='scope:old-name.b1',
                     physics_tag=self.p, evgen_tag=self.e, simu_tag=self.s, reco_tag=self.r,
                     physics_config=self.pc, created_by='owner', metadata={'fold': ['evidence']})
        Dataset.objects.bulk_create([ds])
        Dataset.objects.filter(pk=ds.pk).update(physics_config=None, composed_name='corrected-name')
        Dataset.objects.filter(pk=ds.pk).delete()
        events = IdentityHistory.objects.filter(table_name='pcs_dataset', record_id=ds.pk)
        self.assertEqual(list(events.values_list('operation', flat=True)), ['INSERT', 'UPDATE', 'DELETE'])
        self.assertEqual(events[1].before['physics_config_id'], self.pc.pk)
        self.assertEqual(events[2].before['metadata'], {'fold': ['evidence']})
        self.assertTrue(PhysicsConfig.objects.filter(pk=self.pc.pk).exists())

    def api(self, action, method='post', data=None, user=None, path='/'):
        request = getattr(APIRequestFactory(), method)(path, data or {}, format='json')
        force_authenticate(request, user=user or self.user)
        return EvgenTagViewSet.as_view({method: action})(request, **({'tag_number': 1} if action != 'list' else {}))

    def test_api_delete_routes_refuse_and_lifecycle_keeps_detail_accessible(self):
        self.assertEqual(self.api('soft_delete').status_code, 405)
        self.assertEqual(self.api('destroy', method='delete').status_code, 405)
        self.assertEqual(self.api('lifecycle', data={'lifecycle': 'retired', 'reason': 'Unused draft'}).status_code, 200)
        self.assertEqual(self.api('retrieve', method='get').status_code, 200)
        listed = self.api('list', method='get').data
        self.assertEqual(listed.get('results', []) if isinstance(listed, dict) else listed, [])
        listed = self.api('list', method='get', path='/?include_retired=1').data
        self.assertEqual(len(listed.get('results', []) if isinstance(listed, dict) else listed), 1)
        self.assertEqual(self.api('partial_update', method='patch', data={'description': 'bad'}).status_code, 400)
        self.assertEqual(self.api('lock').status_code, 400)

    def test_disposition_permissions_and_reason_required(self):
        other = get_user_model().objects.create_user(username='other')
        self.assertEqual(self.api('lifecycle', user=other, data={'lifecycle': 'retired', 'reason': 'No'}).status_code, 403)
        self.assertEqual(self.api('lifecycle', data={'lifecycle': 'retired'}).status_code, 400)
        self.assertEqual(self.api('lifecycle', data={'lifecycle': 'superseded', 'reason': 'No target'}).status_code, 400)
        self.assertEqual(self.client.post(reverse('pcs:tag_delete', args=['e', 1])).status_code, 405)

    def test_html_history_retains_retired_pc_without_editions(self):
        set_lifecycle(self.pc, 'retired', reason='Preserve <original> identity', changed_by='owner')
        response = self.client.get(reverse('pcs:identity_detail', args=['pc', 'pc1']))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Preserve &lt;original&gt; identity')
        self.assertContains(response, 'EDT')
        response = self.client.get(reverse('pcs:pcs_config_detail', args=['pc1']))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Disposition &amp; history')
        response = self.client.get(reverse('pcs:tag_compose', args=['k']))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Disposition &amp; history')

    def test_automatic_matching_excludes_retired_tags(self):
        from pcs.services import find_or_create_evgen_tag
        set_lifecycle(self.e, 'retired', reason='Bad definition', changed_by='owner')
        tag, action = find_or_create_evgen_tag({'generator': 'test'}, dry_run=True)
        self.assertIsNone(tag)
        self.assertEqual(action, 'create')

    def test_runtime_role_cannot_bypass_or_forge_history(self):
        with connection.cursor() as cursor:
            cursor.execute('CREATE ROLE pcs_test_runtime NOLOGIN')
            cursor.execute('GRANT USAGE ON SCHEMA public TO pcs_test_runtime')
            cursor.execute('GRANT SELECT, INSERT, UPDATE ON pcs_evgen_tag TO pcs_test_runtime')
            cursor.execute('GRANT USAGE ON SEQUENCE pcs_evgen_tag_id_seq TO pcs_test_runtime')
            cursor.execute('GRANT SELECT ON pcs_identity_history TO pcs_test_runtime')
            cursor.execute('SET LOCAL ROLE pcs_test_runtime')
        try:
            self.assert_sql_rejected('ALTER TABLE pcs_evgen_tag DISABLE TRIGGER ALL')
            self.assert_sql_rejected('DROP TABLE pcs_evgen_tag CASCADE')
            self.assert_sql_rejected('TRUNCATE pcs_evgen_tag CASCADE')
            self.assert_sql_rejected('DELETE FROM pcs_evgen_tag')
            self.assert_sql_rejected('DELETE FROM pcs_identity_history')
            self.assert_sql_rejected("INSERT INTO pcs_identity_history (table_name) VALUES ('forged')")
            self.assert_sql_rejected("SET LOCAL session_replication_role='replica'")
            with connection.cursor() as cursor:
                cursor.execute("UPDATE pcs_evgen_tag SET description='permitted correction'")
                cursor.execute("SELECT after->>'description' FROM pcs_identity_history WHERE table_name='pcs_evgen_tag' ORDER BY id DESC LIMIT 1")
                self.assertEqual(cursor.fetchone()[0], 'permitted correction')
        finally:
            with connection.cursor() as cursor:
                cursor.execute('RESET ROLE')
