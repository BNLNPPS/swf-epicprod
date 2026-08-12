# Composed-Name Integrity: Collision Repair and Doors Closed

Plan of record for repairing composed-name degeneracy in the PCS
catalog and preventing its recurrence. Written 2026-08-11 after a
production incident: a restart aimed at PanDA task 38541 executed
against task 38558, because both tasks' datasets compose to the same
name. The composed tag name is the task identity in every URL, API
lookup, and MCP tool ([PCS.md](PCS.md)); this document restores its
uniqueness and closes the paths that let it degrade.

All four steps were completed 2026-08-11: the resolver (ambiguity
refusal and base resolution on every surface), the intake guard, the
backfill (family dispositions in
[PCS_COMPOSED_NAME_FAMILIES.md](PCS_COMPOSED_NAME_FAMILIES.md);
5,803 datasets repaired in two passes, 77 physics rebinds all reusing
existing tags, two OVERLAY background tags, five EVGEN rebinds, the
rest samples), and the standing invariant check. Composed-name
collisions catalog-wide after completion: zero. Step 4 was
implemented as monitoring only — the `composed-name-integrity` System
page collector with a daily Capcom alarm while broken. The DB
constraint was dropped: it would give a missed case crash semantics
inside nightly writers that are not collision-aware, where the
collector gives detection within a cycle and curation semantics
(operator decision).

## The incident and its mechanism

PanDA task 38541 (`...DIS.NC.10x100.minQ2-1`, aborted) and task 38558
(`...DIS.NC.10x100.minQ2-10`, running) belong to different datasets
that compose to the same name,
`group.EIC.26.07.1.epic_craterlake.p2365.e1.s1.r1`. The task page's
name link resolves the composed name, `resolve_prodtask`
(`pcs/services.py`) returns the first match for an ambiguous name, and
the operator's Restart and retry failures action ran against the wrong
sibling. JEDI refused the retry (running tasks are not retryable), so
no production harm resulted.

## Forensics: how the catalog degraded

Audit of 2026-08-11, all ProdTasks:

| Measure | Count |
|---|---|
| ProdTasks | 6,413 |
| Distinct composed names | 3,343 |
| Colliding names | 1,477 |
| — spanning distinct datasets (identity collisions) | 1,399 |
| — same dataset, multiple attempts | 78 |
| Datasets with a sample_name | 416 (26.02.0: 250, 26.06.0: 166) |
| Collisions among sample-named datasets | 0 |

The `sample_name` mechanism works wherever it is applied: every
collision involves datasets whose `sample_name` is empty. The empty
fields trace to one path. `intake_direct_panda_task`
(`pcs/services.py`) — the association sweep's auto-intake of
direct-to-PanDA submissions, added under the commissioning policy that
everything lands in the catalog — creates each dataset with a physics
tag from `derive_physics()`, which extracts process and beam energies
but not the finer discriminators (minQ2, q2 ranges) present in the
task name, and with no `sample_name` at all. Nothing in the model or
any intake path enforces composed-identity uniqueness (the only
dataset uniqueness is the legacy `(dataset_name, block_num)`), and no
audit watched for collisions, so the degradation was silent and
cumulative. The curated intake paths (questionnaire import, campaign
instancing) carry sample names from their sources and produced the
416 collision-free sample-named datasets; the entire 26.07.1 campaign
(487 datasets) arrived through auto-intake and carries none.

The validation REST interface (`v1/samples/<sample>/completion/`) keys
on the composed name through the same resolver and therefore serves an
arbitrary sibling for a colliding name. No external validation system
consumes it yet; the resolver repair below fixes it with everything
else.

## Repair plan

The sample segment is the default discriminator: it changes no tag
identity, which the Snapper per-PC record and the automatch depend on.
A collision family may instead warrant a new physics tag where the
combinatorics favor one — a discriminator that recurs structurally
across many datasets rather than labeling a scan point — decided per
family in the backfill dry-run review (decision 2026-08-11).

1. **Resolver: refuse ambiguity, resolve staleness** —
   `resolve_prodtask` (and `resolve_dataset`) stop returning an
   arbitrary match. An ambiguous composed name returns the full match
   set; the compose page presents it as a disambiguation list (task,
   physical name, attempts), the API surfaces answer 300-style with
   the candidate list. A name that no longer matches directly is
   reduced to its composed base and resolved to the sample-suffixed
   descendants, so pre-repair URLs, notices, and bookmarks present
   the matching tasks instead of going stale. One resolver serves the
   compose page, the MCP tools, and the validation API, so every
   surface inherits both behaviors.
2. **Auto-intake derives the sample** — `intake_direct_panda_task`
   sets `sample_name` from the physical-name remainder left after
   physics derivation (`minQ2-1`, `q2_100to1000`, and similar, subject
   to the reserved-token check). A remainder that yields no discriminator
   and would collide is flagged at intake rather than stored silently.
3. **Backfill** — one pass over the ~3,280 sample-less datasets
   derives `sample_name` the same way, keeping the stored
   `composed_name` column in sync. The collision audit is the
   worklist; the pass runs dry-run first and reports any name it
   cannot make unique.
4. **Invariant** — after the backfill reaches zero collisions: a DB
   uniqueness constraint on the stored composed identity, an
   intake-time refusal for any path that would violate it, and a
   standing collision audit on the System page that alarms above
   zero. The degradation was invisible for months; it must never be
   silent again.

## Backfill blast radius

The durable records store physical names, not composed ones, so the
rename is contained:

- PanDA associations key on `jedi_task_id` and store physical
  `task_name`/`out_ds` — untouched.
- Legacy name matching resolves through the stored physical name —
  untouched.
- Rucio data lives under physical names — untouched.
- The delivery record and Snapper quilt are per-PC (physics tags are
  not changed) — untouched.
- Old composed-name URLs and API keys are handled by the resolver's
  base-reduction (step 1), so they present the renamed tasks rather
  than failing.
- Validation-interface consumers: none exist yet; the catalog
  endpoint enumerates current names for when they do.

## The structural conclusion

Auto-intake exists because current-campaign production is submitted
directly to PanDA and cataloged after the fact; the catalog cannot
guarantee identities it only learns retroactively. 26.07 is the
transitional campaign; the next campaign's work should be injected
through PCS (request → submit), with usability improvements to the web
interface and CLI as production operators need them
([PCS_DATASET_REQUEST_WORKFLOW.md](PCS_DATASET_REQUEST_WORKFLOW.md),
[JEDI_INTEGRATION.md](JEDI_INTEGRATION.md)). Auto-intake then returns
to its intended role: an exception path for stragglers, guarded by the
same invariant as every other intake.
