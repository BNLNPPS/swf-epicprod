"""Irreversible: an issued identity and its recorded history never disappear."""
from django.db import migrations


TABLES = ('pcs_physics_tag', 'pcs_evgen_tag', 'pcs_simu_tag', 'pcs_reco_tag',
          'pcs_background_tag', 'pcs_physics_config')

FUNCTIONS = r'''
CREATE FUNCTION public.pcs_reject_erasure() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    RAISE EXCEPTION 'PCS permanent record: % on % is forbidden', TG_OP, TG_TABLE_NAME
        USING ERRCODE = '23514';
END;
$$;

CREATE FUNCTION public.pcs_guard_identity() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
DECLARE
    old_row jsonb; new_row jsonb := to_jsonb(NEW); field text;
    lifecycle_changed boolean; replacement_state text; cycle_found boolean;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        old_row := to_jsonb(OLD);
        FOREACH field IN ARRAY TG_ARGV LOOP
            IF old_row -> field IS DISTINCT FROM new_row -> field THEN
                RAISE EXCEPTION 'PCS permanent identity: %.% cannot change', TG_TABLE_NAME, field
                    USING ERRCODE = '23514';
            END IF;
        END LOOP;
        IF old_row ->> 'status' = 'locked' AND
           (old_row -> 'status' IS DISTINCT FROM new_row -> 'status' OR
            old_row -> 'parameters' IS DISTINCT FROM new_row -> 'parameters') THEN
            RAISE EXCEPTION 'PCS locked tag: create a new tag for changed parameters'
                USING ERRCODE = '23514';
        END IF;
    END IF;
    lifecycle_changed := TG_OP = 'INSERT' OR
        (old_row -> 'lifecycle', old_row -> 'lifecycle_reason', old_row -> 'lifecycle_by',
         old_row -> 'lifecycle_at', old_row -> 'superseded_by_id') IS DISTINCT FROM
        (new_row -> 'lifecycle', new_row -> 'lifecycle_reason', new_row -> 'lifecycle_by',
         new_row -> 'lifecycle_at', new_row -> 'superseded_by_id');
    IF lifecycle_changed THEN
        IF NEW.lifecycle NOT IN ('active', 'retired', 'invalid', 'superseded') OR
           ((TG_OP = 'UPDATE' OR NEW.lifecycle <> 'active') AND
            (btrim(NEW.lifecycle_reason) = '' OR btrim(NEW.lifecycle_by) = '' OR NEW.lifecycle_at IS NULL)) OR
           ((NEW.lifecycle = 'superseded') <> (NEW.superseded_by_id IS NOT NULL)) THEN
            RAISE EXCEPTION 'PCS disposition requires a valid state, reason, actor, time and supersession target'
                USING ERRCODE = '23514';
        END IF;
        -- Serialize link changes across these six small identity tables.
        PERFORM pg_advisory_xact_lock(73727, 1);
        IF NEW.superseded_by_id IS NOT NULL THEN
            EXECUTE format('SELECT lifecycle FROM public.%I WHERE id = $1', TG_TABLE_NAME)
                INTO replacement_state USING NEW.superseded_by_id;
            IF replacement_state IS DISTINCT FROM 'active' OR NEW.superseded_by_id = NEW.id THEN
                RAISE EXCEPTION 'PCS supersession target must be a different active identity'
                    USING ERRCODE = '23514';
            END IF;
            EXECUTE format('WITH RECURSIVE chain AS (
                SELECT id, superseded_by_id FROM public.%I WHERE id = $1
                UNION SELECT t.id, t.superseded_by_id FROM public.%I t
                JOIN chain c ON t.id = c.superseded_by_id)
                SELECT EXISTS (SELECT 1 FROM chain WHERE id = $2)', TG_TABLE_NAME, TG_TABLE_NAME)
                INTO cycle_found USING NEW.superseded_by_id, NEW.id;
            IF cycle_found THEN
                RAISE EXCEPTION 'PCS supersession cannot form a cycle' USING ERRCODE = '23514';
            END IF;
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION public.pcs_record_identity() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE old_row jsonb; new_row jsonb; actor_name text; reason_text text;
BEGIN
    IF TG_OP <> 'INSERT' THEN old_row := to_jsonb(OLD); END IF;
    IF TG_OP <> 'DELETE' THEN new_row := to_jsonb(NEW); END IF;
    IF TG_OP = 'UPDATE' AND old_row - 'updated_at' = new_row - 'updated_at' THEN RETURN NEW; END IF;
    IF TG_TABLE_NAME = 'pcs_dataset' AND TG_OP = 'UPDATE' AND
       old_row - ARRAY['file_count', 'data_size'] = new_row - ARRAY['file_count', 'data_size'] THEN
        RETURN NEW;
    END IF;
    actor_name := coalesce(nullif(current_setting('pcs.actor', true), ''),
        CASE WHEN TG_OP = 'INSERT' THEN nullif(new_row ->> 'created_by', '') END, session_user);
    reason_text := coalesce(current_setting('pcs.reason', true), '');
    IF TG_TABLE_NAME <> 'pcs_dataset' AND TG_OP = 'UPDATE' AND
       old_row -> 'lifecycle_at' IS DISTINCT FROM new_row -> 'lifecycle_at' THEN
        actor_name := new_row ->> 'lifecycle_by';
        reason_text := new_row ->> 'lifecycle_reason';
    END IF;
    INSERT INTO public.pcs_identity_history
        (table_name, record_id, operation, before, after, changed_at, database_user, actor, reason)
    VALUES (TG_TABLE_NAME, coalesce(NEW.id, OLD.id), TG_OP, old_row, new_row,
            clock_timestamp(), session_user, actor_name, reason_text);
    RETURN coalesce(NEW, OLD);
END;
$$;
REVOKE ALL ON FUNCTION public.pcs_reject_erasure() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.pcs_guard_identity() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.pcs_record_identity() FROM PUBLIC;
'''


def protect(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        raise RuntimeError('PCS permanent identities require PostgreSQL enforcement.')
    schema_editor.execute(FUNCTIONS, params=None)
    for table in TABLES:
        identity_fields = ['id', 'created_at', 'created_by']
        if table == 'pcs_physics_config':
            identity_fields += ['label', 'config_key', 'physics_tag_id',
                                'background_tag_id', 'sample_name', 'evgen_display']
        else:
            identity_fields += ['tag_number', 'tag_label']
            if table == 'pcs_physics_tag':
                identity_fields += ['category_id']
        args = ', '.join("'%s'" % field for field in identity_fields)
        schema_editor.execute(f'''
            CREATE TRIGGER pcs_no_delete BEFORE DELETE ON public.{table}
                FOR EACH STATEMENT EXECUTE FUNCTION public.pcs_reject_erasure();
            CREATE TRIGGER pcs_no_truncate BEFORE TRUNCATE ON public.{table}
                FOR EACH STATEMENT EXECUTE FUNCTION public.pcs_reject_erasure();
            CREATE TRIGGER pcs_identity_guard BEFORE INSERT OR UPDATE ON public.{table}
                FOR EACH ROW EXECUTE FUNCTION public.pcs_guard_identity({args});
            ALTER TABLE public.{table} ENABLE ALWAYS TRIGGER pcs_no_delete;
            ALTER TABLE public.{table} ENABLE ALWAYS TRIGGER pcs_no_truncate;
            ALTER TABLE public.{table} ENABLE ALWAYS TRIGGER pcs_identity_guard;
        ''')
    # Edition snapshots retain old names, PC bindings and metadata even when a
    # placeholder edition is legitimately folded. Edition deletion remains allowed.
    for table in (*TABLES, 'pcs_dataset'):
        schema_editor.execute(f'''
            INSERT INTO public.pcs_identity_history
                (table_name, record_id, operation, before, after, changed_at, database_user, actor, reason)
            SELECT '{table}', id, 'BASELINE', NULL, to_jsonb(t), clock_timestamp(),
                session_user, session_user, 'Permanent identity protection installed'
            FROM public.{table} t;
            CREATE TRIGGER pcs_identity_record AFTER INSERT OR UPDATE OR DELETE ON public.{table}
                FOR EACH ROW EXECUTE FUNCTION public.pcs_record_identity();
            ALTER TABLE public.{table} ENABLE ALWAYS TRIGGER pcs_identity_record;
        ''')
    schema_editor.execute('''
        CREATE TRIGGER pcs_no_truncate BEFORE TRUNCATE ON public.pcs_dataset
            FOR EACH STATEMENT EXECUTE FUNCTION public.pcs_reject_erasure();
        ALTER TABLE public.pcs_dataset ENABLE ALWAYS TRIGGER pcs_no_truncate;
        CREATE TRIGGER pcs_history_append_only BEFORE UPDATE OR DELETE OR TRUNCATE
            ON public.pcs_identity_history FOR EACH STATEMENT
            EXECUTE FUNCTION public.pcs_reject_erasure();
        ALTER TABLE public.pcs_identity_history ENABLE ALWAYS TRIGGER pcs_history_append_only;
        REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON public.pcs_identity_history FROM PUBLIC;
    ''')


class Migration(migrations.Migration):
    dependencies = [('pcs', '0009_permanent_identities')]
    operations = [migrations.RunPython(protect)]
