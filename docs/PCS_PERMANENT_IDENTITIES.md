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

## Deployment and recovery

BNL's existing database backups provide recovery. No separate PCS archive,
archive account or independent storage destination is required.

Activation requires an explicitly approved full deployment: migrations
`0009` and `0010`, restricted runtime role provisioning, credential cutover,
and service restart. Applications use `swf_runtime`; `pcs_identity_owner`
is NOLOGIN and owns the six identity tables, history and trigger functions.
Operator accounts remain available: switching application credentials does not
authorize disabling an operator login, removing its password or changing its
role attributes. The `wenaus` login retains its original password and CREATEDB,
with read/write access to protected identities and read access to history.
The deletion and identity guards still apply. No owner membership is granted
to the application runtime role.

The full deploy uses `swf-monitor/scripts/migrate-swfdb.py` as root to run
migrations through the local PostgreSQL administrator's peer authentication.
No administrator password is stored in the application environment. Future
schema migrations use this same path; the application login stays restricted.

Apply role provisioning as PostgreSQL administrator after the migrations:

```sh
psql -X --set=runtime_role=swf_runtime --set=owner_role=pcs_identity_owner \
  --set=migration_role=postgres --file=scripts/pcs-permanent-roles.sql swfdb
```

The script fails on existing role names rather than inheriting unknown grants.
After switching the environments and restarting services, the actual runtime
connection must pass `python src/manage.py pcs_verify_permanence`. That command
checks owner membership, role and schema privileges, forbidden table privileges
and all required ALWAYS triggers. The runtime can read sequence values so the
existing `backup-swfdb.sh` can continue to dump the database, but cannot advance
or reset the history sequence. Reverting application code must retain the
protections; never reverse migrations to bring back a deletion feature.

For recovery, restore a verified BNL/full database backup into a fresh isolated
database first. Never run a destructive restore over the live catalog. Compare
original IDs, labels, defining fields and history, then prepare explicit
missing-row repairs or a reviewed recovery cutover. Preserve surviving edition
bindings and every existing identity; do not renumber or silently overwrite
conflicting rows. Run the runtime protection check before returning to service.

Owners, superusers and infrastructure administrators can ultimately remove
storage or protections. Restricted application privileges prevent ordinary
code and maintenance mistakes; BNL database backups provide disaster recovery.

## Verification

Run `python src/manage.py test pcs.test_permanence` on an isolated PostgreSQL
server. The tests use rollback-isolated Django TestCase transactions and cover
all six types, ORM/raw deletion, truncation, identity reassignment, replication
mode, history tampering, edition folds, lifecycle rules, automatic matching,
web/API historical lookup and a restricted runtime role.
