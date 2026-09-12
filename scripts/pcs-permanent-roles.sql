-- Apply only during the approved deployment, as a database administrator.
-- psql --set=runtime_role=swf_runtime --set=owner_role=pcs_identity_owner \
--      --set=migration_role=postgres \
--      --file=scripts/pcs-permanent-roles.sql <connection options>
-- Role names are quoted as identifiers; no passwords appear in this file.
\set ON_ERROR_STOP on
BEGIN;
-- Fail on collisions: reusing a role without auditing its grants is unsafe.
CREATE ROLE :"owner_role" NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE ROLE :"runtime_role" NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
SELECT format('GRANT CONNECT ON DATABASE %I TO %I', current_database(), :'runtime_role') \gexec
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO :"runtime_role", :"owner_role";
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO :"runtime_role";
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO :"runtime_role";
ALTER DEFAULT PRIVILEGES FOR ROLE :"migration_role" IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO :"runtime_role";
ALTER DEFAULT PRIVILEGES FOR ROLE :"migration_role" IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO :"runtime_role";
-- No role membership in the protected owner is granted to either reader or writer.
SELECT format('ALTER TABLE public.%I OWNER TO %I', t, :'owner_role')
FROM unnest(ARRAY['pcs_physics_tag','pcs_evgen_tag','pcs_simu_tag','pcs_reco_tag',
                 'pcs_background_tag','pcs_physics_config','pcs_identity_history']) t \gexec
SELECT format('ALTER FUNCTION public.%I() OWNER TO %I', f, :'owner_role')
FROM unnest(ARRAY['pcs_reject_erasure','pcs_guard_identity','pcs_record_identity']) f \gexec
REVOKE DELETE, TRUNCATE, REFERENCES, TRIGGER ON
    public.pcs_physics_tag, public.pcs_evgen_tag, public.pcs_simu_tag,
    public.pcs_reco_tag, public.pcs_background_tag, public.pcs_physics_config
    FROM :"runtime_role", PUBLIC;
REVOKE ALL ON public.pcs_identity_history FROM :"runtime_role", PUBLIC;
GRANT SELECT ON public.pcs_identity_history TO :"runtime_role";
REVOKE ALL ON SEQUENCE public.pcs_identity_history_id_seq FROM :"runtime_role", PUBLIC;
-- The existing database backup job uses the runtime login for pg_dump.
-- SELECT reads sequence state; it cannot advance or reset the history sequence.
GRANT SELECT ON SEQUENCE public.pcs_identity_history_id_seq TO :"runtime_role";
COMMIT;
-- Activate the runtime LOGIN and configure authentication, then change DB_USER
-- for every application/worker/maintenance process. Never distribute owner credentials.
