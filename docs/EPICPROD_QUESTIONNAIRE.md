# ePIC Production Request Questionnaire

Physics working groups and detector groups (PWG/DSC) request production datasets
through a Google Form. The Physics Configuration System (PCS) mirrors each response into a
Questionnaire record, giving the collaboration a browsable view of all requests
and giving downstream production records a single upstream entry to reference for
request provenance. This document describes the entity, its ingestion from the
form, its relationship to the existing request and task records, and its access
model.

This should be read as proposal, not established design.

The request and task records and the shared intake layer are described in
[PCS_DATASET_REQUEST_WORKFLOW.md](PCS_DATASET_REQUEST_WORKFLOW.md). The external
read/write contract referenced below is in [EXTERNAL_ACCESS.md](https://github.com/BNLNPPS/swf-monitor/blob/main/docs/EXTERNAL_ACCESS.md).

## Not a google form replacement (at this point at least)

Requesters continue to use the Google Form. PCS ingests it and does not replace it
or require any new submission tool. Responsibility for the integrity of a
submission stays with the form: a Questionnaire record reflects what the form
holds and asserts no independent authority over the submitted content. 

## The Questionnaire entity

A Questionnaire record is a read-only mirror of one form response. The mirrored
fields are not edited in PCS. A `data` JSON metadata field, following the convention used by the other PCS models, is added and additional material is put there, e.g. annotation notes on the request. A status string field is also added for ops use. 

The Questionnaire is distinct from the request record (`ProdRequest`). The
Questionnaire is the request as submitted. `ProdRequest` is the triaged production
record composed from PCS physics, generator, simulation, and reconstruction tags
and a production configuration. The Questionnaire is not an extension of
`ProdRequest`; the two are separate entities with separate authorities and
audiences.

## Mirrored fields

The form has seven response columns. Each maps to a Questionnaire field:

| Field | Source question |
|---|---|
| `submitted_at` | Timestamp |
| `description` | Name the dataset, generator, and purpose |
| `repository` | Location of the repository with version control enforced per the input-processing guidelines (e.g. a tagged generator or steering-file repository) |
| `contact` | Who is the contact person for the dataset generation |
| `nevents` | How many events are requested |
| `benchmark` | Time to simulate the first 100 events and disk space the output file occupies |
| `estimate` | Total compute or storage required for the dataset, with justification for requests above 1% of the campaign budget (~120 core-years and ~35 TB per month) |

The `description`, `repository`, and `contact` values are free text from the
submitter. `nevents`, `benchmark`, and `estimate` are submitter-supplied and are
not validated against production at ingestion.

## Ingestion

The source is a CSV export of the form's responses sheet. The export URL is
provided to the fetcher — as configuration or through the logged-in ingest
control — and for now must be an accessible (link-readable) export; authenticated
retrieval is deferred. A scheduled cron job fetches the export, compares it against the
records already ingested, and creates a Questionnaire record for each new
response. A logged-in operator can also trigger the fetch on demand rather than
wait for the next scheduled run.

Ingestion is idempotent on the submission timestamp. A content hash of the
response detects an in-form edit, so a corrected response re-syncs the existing
record in place. Records become active on ingestion; there is no separate review
state, because submission integrity remains with the form.

Ingestion is performed through a `questionnaire_intake` service function in
`pcs.services`, exposed as peer REST and MCP (Model Context Protocol) operations,
matching the existing PCS intake surface in
[PCS_DATASET_REQUEST_WORKFLOW.md](PCS_DATASET_REQUEST_WORKFLOW.md). The CSV fetcher
is one client of that service.

## Submission via PCS (deferred)

A native no-login submission path in PCS is deferred. The form already accepts
requests without a login, which is its purpose; reproducing that in PCS would add
authentication-free write protection, server-side validation, and abuse control
for a capability that already exists. Because ingestion goes through the
`questionnaire_intake` service, a native form added later would call the same
service and leave the ingestion model unchanged.

## Relationship to requests and tasks

`ProdRequest` gains a nullable foreign key to the Questionnaire. One response can
map to several requests, because a single submission frequently spans multiple
beam energies or Q² ranges; a response that has not been triaged maps to none.

A logged-in triage action links a response to a request
(`questionnaire_link_request`, exposed as peer REST and MCP operations).
Composing the request from PCS tags and configuration is a separate triage step on
top of the link.

Downstream provenance is by reference rather than by copy. `ProdTask` references
`ProdRequest`, which references the Questionnaire, so a task resolves to its
originating contact, benchmark, and estimate without duplicating those values. A
specific field is denormalized onto `ProdRequest` when it becomes a query filter
or dispatch key, not before.

For questionnaire-to-production-task matches, the same boundary applies. When an
operator establishes a match, PCS records the link and exposes navigation from
the task UI back to the request, and code can follow that link to retrieve the
request contact/email if needed for notifications or other workflow. The contact
data itself remains on the request/questionnaire record and is not copied onto
`ProdTask`, at least until matches are validated enough to treat them as
authoritative task metadata.

A match binds to the physics configuration, not to one campaign's task
(CAMPAIGN_CONTINUUM.md — requests over physics configurations). Each accepted
match record carries `pc_anchor`, the composed name of the matched task's
dataset — written at match time and self-healed on every cache rebuild — and
the task-local cache (`ProdTask.overrides['questionnaire_matches']`) attaches
the match to every task whose dataset resolves to the same configuration. A
match recorded against one campaign's edition is therefore inherited by every
other edition of the same physics, including editions minted later, without
re-matching. Unresolved configurations key uniquely and never fan out.

## Access and contact handling

The Questionnaire browser is readable by the collaboration without a login, on
both the internal face and the external face served through the swf-remote proxy.
On the internal face this uses the anonymous-allowed configuration already applied
to the open PCS API paths; on the external face the browser page requires a
swf-remote route entry, per the enumeration contract in
[EXTERNAL_ACCESS.md](https://github.com/BNLNPPS/swf-monitor/blob/main/docs/EXTERNAL_ACCESS.md). This design adds no public write path.

Contact-person redaction is computed server-side, in REST serialization, keyed on
the resolved reader identity (the `X-Remote-User` forwarded by swf-remote on the
external face):

- An authenticated reader receives the full contact value.
- An unauthenticated reader receives initials.
- When the contact value is an email address, an unauthenticated reader receives
  only the local part before `@`; the domain is removed and a full email address
  is never returned to an unauthenticated reader.

Redaction is server-side rather than display-side so that neither the REST surface
nor the external proxy emits a full name or email address to an unauthenticated
caller. Response text originates from form submitters, is treated as untrusted
input, and is sanitized when rendered.

## Implementation outline

1. Questionnaire model: the mirrored fields above, a `data` JSON field holding
   annotation notes and other ops material, a `status` string field for ops use,
   the submission timestamp and content hash for idempotent ingestion, and
   creation and update timestamps.
2. `questionnaire_intake` service function, idempotent on the timestamp and
   updating on a content-hash change, with peer REST and MCP operations.
3. CSV fetcher: takes the responses-sheet export URL as provided input (config or
   the operator ingest control); runs on a cron schedule and from a logged-in
   operator trigger. For now the provided URL must be an accessible (link-readable)
   export; authenticated retrieval is deferred.
4. Questionnaire browser and compose page in the two-pane PCS pattern, with
   server-side contact redaction and render-time sanitization, and the external
   route added to swf-remote.
5. `ProdRequest.questionnaire` foreign key and the `questionnaire_link_request`
   triage action.
6. Denormalization of selected fields onto `ProdRequest` as query needs arise.
   Data-flow and entity diagrams once the implementation exists.

## Automated matching

Matching a request to the production tasks that realize it is a standing
process, not a one-shot: `scripts/match-questionnaires.py`, run by the ops
agent as the `questionnaire_automatch` step of the nightly catalog sync (and
on demand). The request side is free text; the task side is composed names
built from tag codes. The delegate model (env `EPICPROD_MATCHER_MODEL`,
default Opus 4.8) is handed the complete tag map inline — every
physics/evgen/simu/reco tag with its actual content — plus the task catalog
and a batch of requests per call, under two hard rules: tag codes are
opaque sequential ids whose numerals mean nothing (the first run, given
names only, matched on digit coincidences), and beam energies and species
must match exactly — 9x100 never matches 10x100, the nearest beam is never
proposed (an early run accepted cross-beam matches at high confidence).
Deterministic guards keep the proposals safe:

- proposed names must resolve against the actual catalog (unresolvable
  proposals are counted and dropped);
- beam exactness is enforced in code, not just in the prompt: the model
  states the request's beams per match, the task's beams come from its
  physics tag, and any inequality drops the proposal (`beam_rejected`); a
  proposal whose beams cannot be verified (request states none, or the tag
  carries none) can land only as `suggested`, never `accepted`;
- existing matches are never modified and matched pairs are never
  re-proposed — the matcher is strictly additive;
- confidence gates status: high/medium land as `accepted` (counted in the
  catalog, removable on the request page), `low` lands as `suggested`
  (visible on the request page, never counted).

Every new match logs a `questionnaire_match_found` action-stream event
(normal, live) carrying the request id, confidence, and the one-line reason —
new matches surface in the live channels as they are found, and a wrong one
is one click to remove (itself a live event). Rescanning is event-driven,
never habitual: an LLM re-asked an unchanged question answers with variance,
not information — noise in the live stream and wasted tokens. Each
questionnaire carries a stamp of what it was last scanned against
(`data['automatch_scan']`: prompt version, task-catalog high-water id,
request content hash) and is asked again only when one of those inputs
changed — the request was edited, the catalog grew, or the matcher prompt
was revised (a `PROMPT_VERSION` bump earns every questionnaire one
deliberate re-pass). A night with no changes makes no LLM call and emits no
events. `Questionnaire.data['prod_matches']` remains the editable source of
truth; the task-side cache rebuild (`rebuild_questionnaire_match_cache`)
runs after any additions.

## Related

- [PCS_DATASET_REQUEST_WORKFLOW.md](PCS_DATASET_REQUEST_WORKFLOW.md) — the request and task records and the shared intake surface.
- [PCS.md](PCS.md) — the configuration and campaign record.
- [EXTERNAL_ACCESS.md](https://github.com/BNLNPPS/swf-monitor/blob/main/docs/EXTERNAL_ACCESS.md) — the external proxy and its read/write contract.
- [EPICPROD_TASK_CATALOG.md](EPICPROD_TASK_CATALOG.md), [EPICPROD_DATA_LINEAGE.md](EPICPROD_DATA_LINEAGE.md) — the downstream catalog and produced-data references.
