"""Read-only deployment gate for PCS identity and privilege protection."""
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from pcs.permanence import PERMANENT_TABLES


class Command(BaseCommand):
    help = 'Verify permanent identity triggers and the current runtime role (read-only).'

    def handle(self, **options):
        if connection.vendor != 'postgresql':
            raise CommandError('PostgreSQL is required.')
        problems = []
        expected = {table: {'pcs_no_delete', 'pcs_no_truncate', 'pcs_identity_guard',
                            'pcs_identity_record'} for table in PERMANENT_TABLES}
        expected['pcs_identity_history'] = {'pcs_history_append_only'}
        expected['pcs_dataset'] = {'pcs_no_truncate', 'pcs_identity_record'}
        with connection.cursor() as cursor:
            cursor.execute('SELECT current_user, rolsuper, rolcreaterole, rolcreatedb FROM pg_roles WHERE rolname=current_user')
            role, *dangerous = cursor.fetchone()
            if any(dangerous):
                problems.append(f'{role} must not be superuser, CREATEROLE or CREATEDB')
            cursor.execute("SELECT pg_has_role(current_user, datdba, 'MEMBER') FROM pg_database WHERE datname=current_database()")
            if cursor.fetchone()[0]:
                problems.append(f'{role} owns or can assume the database owner')
            cursor.execute("SELECT has_schema_privilege(current_user, 'public', 'CREATE')")
            if cursor.fetchone()[0]:
                problems.append(f'{role} can create objects in public')
            for table, triggers in expected.items():
                cursor.execute('''SELECT t.tgname FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
                    JOIN pg_namespace n ON n.oid=c.relnamespace
                    WHERE n.nspname='public' AND c.relname=%s AND t.tgenabled='A' AND NOT t.tgisinternal''', [table])
                missing = triggers - {row[0] for row in cursor.fetchall()}
                if missing:
                    problems.append(f'{table}: missing ALWAYS triggers {sorted(missing)}')
                cursor.execute('''SELECT pg_has_role(current_user, c.relowner, 'MEMBER')
                    FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                    WHERE n.nspname='public' AND c.relname=%s''', [table])
                row = cursor.fetchone()
                if row is None or row[0]:
                    problems.append(f'{table}: current role owns, can assume owner, or table is absent')
                forbidden = ['TRUNCATE', 'TRIGGER']
                if table != 'pcs_dataset':
                    forbidden.append('DELETE')
                if table == 'pcs_identity_history':
                    forbidden.extend(['INSERT', 'UPDATE'])
                for privilege in forbidden:
                    cursor.execute('SELECT has_table_privilege(current_user, %s, %s)', [f'public.{table}', privilege])
                    if cursor.fetchone()[0]:
                        problems.append(f'{table}: runtime has {privilege}')
            cursor.execute('''SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
                WHERE n.nspname='public' AND p.proname IN
                ('pcs_reject_erasure','pcs_guard_identity','pcs_record_identity')
                AND pg_has_role(current_user, p.proowner, 'MEMBER')''')
            problems.extend(f'Runtime can assume function owner: {row[0]}' for row in cursor.fetchall())
        if problems:
            raise CommandError('\n'.join(problems))
        self.stdout.write(self.style.SUCCESS(f'PCS permanence verified for runtime role {role}.'))
