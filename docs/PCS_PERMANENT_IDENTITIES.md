# Permanent PCS identities

Every issued p/e/s/r/k tag and PhysicsConfig (`pcNN`) is permanent, even a
draft, mistake, duplicate or configuration without current editions. An
identifier is never recycled. This rule has no commissioning, maintenance,
merge or orphan-cleanup exception.

## Enforcement and lifecycle

Migration `0009_permanent_identities` adds an orthogonal disposition:
`active`, `retired`, `invalid`, `superseded`. The last three remove a tag
from ordinary composition choices and automatic tag matching. Unfiltered ORM
lookups and historical URLs still find it. Draft/locked keeps its existing
meaning; draft corrections remain possible and are recorded. Changing a
locked tag's parameters requires a new tag. Category, issued number, label,
creation provenance and the defining PhysicsConfig fields are immutable.

`pcs.permanence.set_lifecycle` requires an actor and reason, records the time,
and accepts an active replacement of the same type for supersession. A
self-reference or cycle is rejected. Reactivation requires another reason;
it does not unlock a tag. The UI's **Disposition & history** page is available
for all six identity types at `/pcs/identities/<p|e|s|r|k|pc>/<label>/`.
The creator or staff can change disposition; historical reads remain public
like existing tag/configuration pages. Existing references are not migrated
or redirected automatically by retirement or supersession.

Tag REST `POST /pcs/api/<type>-tags/<number>/lifecycle/` accepts:

```json
{"lifecycle": "superseded", "reason": "Corrected generator definition", "replacement": "e62"}
```

Omit `replacement` for other states. List endpoints show active tags by
default, with `?include_retired=1` for all dispositions. Detail retrieval
always includes old identities. Old `/delete/` endpoints and HTTP DELETE
return 405; the application and Django admin provide no tag deletion action.

Migration `0010_identity_guards` is intentionally irreversible:

- Statement triggers reject DELETE and TRUNCATE on all six tables, including
  bulk SQL and cascades. Row triggers reject changes to identity fields.
- All protection and recording triggers are **ENABLE ALWAYS**, including
  replication sessions. Application code, queryset operations, raw SQL and
  maintenance scripts cannot bypass the database guards.
- `pcs_identity_history` records a baseline for existing identities and
  editions, then full before/after snapshots of changes, database login,
  actor, reason and timestamp. Baselines preserve pre-existing JSON history;
  they do not invent earlier events. `identity_change(actor, reason)` adds
  caller attribution within a transaction; otherwise the database login is
  recorded. Nested attribution contexts are restored.
- Edition snapshots also retain old names, configuration bindings and
  metadata through folds and rebindings. Deleting a placeholder edition
  remains permitted and records its final state. Truncating editions is
  forbidden because that would skip the row history.
- History UPDATE, DELETE and TRUNCATE are rejected. A narrowly scoped
  SECURITY DEFINER trigger inserts history; the runtime role cannot insert
  fabricated history or reset its sequence. Its search path is fixed.
- Every unique identifier remains reserved by its retained row. Resetting an
  allocator cannot reuse one: existing unique constraints reject collisions.

The three `scripts/fold_*` global orphan-configuration deletes are removed.
A fold preserves old configuration associations on their original record.
Carrying associations to a successor is an explicit curation operation;
retirement does not guess how to redistribute them.

## Deployment runbook (explicit approval required)

Code preparation and isolated tests do **not** activate these protections in
production. This change needs migrations and a credential cutover, hence a
full deployment rather than a lightweight UI sync.

1. During an approved maintenance window, stop application/agent writers and
   take a fresh full PostgreSQL backup. Retain the pre-change backup and the
   September 12 recovery audit independently; do not expire them as routine
   nightlies. Install the committed package into the deployment environment.
2. Using an operator-held migration/admin connection, apply Django migrations
   through `pcs.0010_identity_guards`. Check that all six tables' row counts
   and issued labels are preserved. Do not use `--fake` or reverse these
   migrations. The migration baselines all existing records before normal
   writing resumes.
3. As a database administrator, run the reviewed role script:

   ```sh
   psql --service=swf_admin -X --set=runtime_role=swf_runtime \
     --set=owner_role=pcs_identity_owner --set=archive_role=pcs_archive_reader \
     --set=migration_role=wenaus --file=scripts/pcs-permanent-roles.sql
   ```

   It fails on existing role names so inherited privileges cannot slip
   through. `pcs_identity_owner` is NOLOGIN and owns the six identity tables,
   history and trigger functions. `swf_runtime` owns none of them and receives
   the ordinary application DML privileges, minus erasure/trigger privileges
   on protected objects. The archive role has SELECT only on the archive
   tables. No owner-role membership is granted to either role. The script is
   transactional and does not put passwords in command lines or files.
4. Provision runtime and archive authentication through the operator's secret
   mechanism, enabling LOGIN on those two roles. Keep the owner role NOLOGIN.
   Change `DB_USER`/credentials for **every** writer: deployed web/ASGI/MCP,
   testbed agents, scheduled jobs and ordinary maintenance shells. Remove
   privileged credentials from their environments, including `~/.env`, and
   rotate the former owner password so stale copies cannot bypass the boundary.
   Administrators retain privileged access outside these accounts. Existing
   backups should use an appropriately restricted backup connection.
5. With the actual runtime connection, run:

   ```sh
   python src/manage.py pcs_verify_permanence
   ```

   This read-only gate must pass before writers restart. It checks role
   attributes, owner membership, schema privileges, protected-table privileges,
   and presence of all expected ALWAYS triggers. It must also run after future
   migrations or grant changes. Future schema migrations use an operator-held
   privileged connection; application processes never run as the owner.
6. Configure the independent receiver below and verify a retained archive.
   Start writers with the runtime credentials. Reverting application code must
   retain these database protections; never reverse migrations to restore a
   deletion feature.

The repository supplies the scripts; login provisioning, credential rotation,
service restart and independent storage activation are deployment operations.
A destination and its independently enforced retention policy must be chosen
before the recovery layer can be called active.

## Independent recovery archive

Run `scripts/archive-pcs-identities.py` on a receiver controlled separately
from the application host. Its libpq service uses `pcs_archive_reader` and a
receiver-owned password file or equivalent authentication. The source host and
its agents get no credentials that can alter or remove receiver archives.

```sh
python archive-pcs-identities.py --service=pcs_archive \
  --destination=/receiver-owned/pcs-identities
```

Schedule it on that receiver (for example every 15 minutes). It exports all
six identity tables, categories, edition records and complete identity history
in one repeatable-read, read-only PostgreSQL snapshot. Files use unique names,
exclusive publication, gzip validation, counts, a completion trailer and a
SHA-256 companion. It never overwrites or ages out an archive. The storage
administrator must enforce retention/immutability independently (e.g. storage
object lock or protected snapshots); file permissions alone are not immutable
storage. Keep full database backups off-host as well for whole-system recovery.

The schedule sets the disaster recovery point: unsent commits can be lost if
the source storage is destroyed between exports. Continuous off-host PostgreSQL
WAL archiving/replication is required for a tighter recovery point; no periodic
archive can honestly promise zero loss under physical destruction.

## Recovery procedure

1. Work on a fresh, isolated recovery database. Never restore with `--clean`
   against the live catalog. Preserve the incident database and its current
   history before any recovery action.
2. Verify the archive's companion with `sha256sum --check`, then `gzip --test`.
   Each JSON line is a `{table, row}` record; the first line identifies the
   format, the last is `complete: true` with per-table counts. The archive
   contains original IDs, labels, timestamps, JSON provenance and associations.
3. Restore the retained **full** database dump into the isolated database with
   `pg_restore --exit-on-error`. Compare its identity tables with the newest
   verified identity archive. Use the archive's complete snapshots and history
   to reconstruct identity changes since that dump in the recovery database.
   The identity archive is evidence for recovery, not a replacement for the
   full dump's campaigns, tasks and other foreign-key dependencies.
4. For selective repair, prepare an explicit inventory of missing identities
   and exact original rows, checking ID/label/key collisions and replacement
   dependencies before any insert. Preserve every surviving edition binding.
   Never silently update an existing different record, invent new IDs for old
   labels, or reset allocators backwards. Record the incident and repair actor.
   Complex supersession histories are reconstructed in the isolated database;
   there is no application-side guard-disable option.
5. Compare every restored identity field and history row against the verified
   source, check references, run `pcs_verify_permanence` with runtime credentials,
   and obtain approval for the reviewed recovery cutover or selective repair.

Owners, superusers and infrastructure administrators can ultimately remove
storage or protections. Separating those credentials from applications and
agents, plus independent retained recovery copies, is the boundary that makes
permanence operationally enforceable.

## Focused verification

Run `python src/manage.py test pcs.test_permanence` against an **isolated
PostgreSQL server**. The suite uses rollback-isolated Django TestCase tests;
it deliberately does not bypass the guards to flush production-like tables.
A disposable server can be removed after testing. Tests cover all six types,
ORM and direct SQL deletion/truncation, identity reassignment, replication
mode, history tampering, correction history, edition folds, lifecycle rules,
automatic matching, web/API historical lookup and a restricted runtime role.
The role-installation SQL and receiver collector must also be exercised on the
isolated server before deployment.
