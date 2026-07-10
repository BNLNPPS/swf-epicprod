# PCS Production Planning Workflow

This note describes the PCS-based workflow for ePIC production
dataset requests. It is both an implementation guide and a
summary to confirm that it matches the production
planning workflow foreseen by the production team and taking shape in `eic/epic-prod`.

## Summary

PCS should be the authoritative record for production 
requests, which take form as
task specifications composed from dataset, tag, and production configuration specs.
The tasks are also the source of record for downstream production state as
production progresses. Mattermost/DISpatcher is a two-way conversational
interface between users and PCS. The `eic/epic-prod` issue/PR/Jekyll workflow is
the public catalogue projection of production plans as expressed in PCS data,
with PCS the source of truth.

The current production reality starts from generator-level files supplied by
PWGs or DSCs, input as CSV file spec manifests. PCS must represent
this present reality, describing
those externally supplied inputs explicitly, while also providing a clean path to a
future mode where EVGEN is run as an internal production stage.

## Current Situation

Sakib has described and prototyped this public intake path:

1. A requester submits a dataset request through a GitHub issue template or,
   later, through Mattermost/DISpatcher as dialog front end to issue creation.
2. A GitHub Action triggered by the issue creation appends a row to `eic/epic-prod`:
   `docs/_data/datasets.csv`.
3. A pull request is opened for review.
4. Once merged, the Jekyll-generated page at the campaign datasets documentation displays
   the new request.

The public CSV currently stores fields such as:

- DSC or PWG
- Dataset Path
- Generator/Dataset Version
- Number of Events
- Background
- New Request
- Pre-TDR Use
- Early Science Use
- Other Use
- Description
- Priority
- Issue

This is metadata that maps onto the PCS components that
compose into either a fully or partially specified production task. 

At present, `Generator/Dataset Version` is a loose string,
not a structured PCS EvGen tag, and `Dataset Path` is an external data source or
manifest, not a tag attribute.

## PCS Authority

PCS is designed to be the authoritative source and record for task configuration:

- request state
- inferred and confirmed metadata
- associated physics, EvGen, simulation, and reconstruction tags
- input and output datasets
- production configuration
- workflow mode and stage structure
- validation status
- public catalogue issue, PR, row, and page references
- user comments
- downstream production status and task IDs

PCS definition and composition into tasks is currently based in a web interface.
Intake goes through a single service layer (`pcs.services`) that is the source
of truth for validation, idempotency, and lifecycle. REST and MCP are peer
surfaces over that layer: each is a thin adapter that turns wire-format input
(HTTP body or MCP args) into a service call and the service result back into
wire-format output.

- **Bots trigger; they do not mediate.** DISpatcher receives a Mattermost
  event, then calls a PCS MCP tool. Bots do not embed PCS logic. Intake
  decisions, validation, and persistence live in the service layer.
- **Web UI uses REST.** PCS web pages call PCS REST for reads and writes;
  nothing in the UI bypasses the service layer.
- **MCP and REST are peers over the same services.** Adding a new
  operation is "add the service function, expose it via REST and MCP" —
  the two surfaces stay aligned because they share business logic.
- **Scripts and other automation** typically call REST (e.g. `pcs-task-cmd`
  is a stdlib HTTP client over REST). They could equally well use MCP;
  the contract is the same.
- **GitHub issue creation, when needed,** is performed by PCS server-side
  on receipt of a 'go' from the bot/user via REST or MCP — programmatically
  precise, so traceability and review are preserved.

User-supplied comments arrive through whichever front-end the user chose
(bot, web, script). PCS records them as part of the request history through
the same service layer. The bot's job is to relay PCS responses (questions,
inferred values, short option lists, validation errors) back to the user;
PCS's job is to compute them.

The intake surface — REST endpoints and the corresponding MCP tools — is
listed under "Intake Surface" below.

## Model Direction

Tags remain reusable attribute sets. They do not carry concrete file paths or
manifests. A concrete data sample is a dataset, described by one or more tags.

PCS should keep one generalized dataset model and extend it to cover externally
supplied data as well as internally produced data. The key additions are:

- stage or role, e.g. `evgen`, `simu`, `reco`, `full`, `log`
- source kind and source location, e.g. CSV manifest, path, URL, Rucio DID, file list

The present externally supplied EVGEN files are then ordinary PCS datasets with
`stage=evgen` and `source.kind=csv_manifest` or another external source kind.
They are described by PCS physics and EvGen tags, but their file/path/CSV
source is dataset metadata, not tag metadata.
Future PCS/PanDA-produced EVGEN outputs are also ordinary datasets, with
`stage=evgen` and an internal production source when that exists.

Interim implementation: externally supplied EVGEN inputs are represented in the
existing `Dataset.metadata` JSON rather than by new database columns. The
metadata convention is:

```json
{
  "stage": "evgen",
  "source": {
    "kind": "csv_manifest",
    "location": "path/to/input.csv"
  }
}
```

This keeps the external-input capability lightweight and transitional while
preserving the path to a later first-class workflow/stage model for EVGEN run
inside PCS/PanDA.

Production tasks should compose lists of datasets rather than a single dataset:

```text
ProdTask
  prod_config
  input_datasets[]
  output_datasets[]
  overrides
  status and submission tracking
```

The current `ProdTask.dataset` is really the output dataset. The current
`ProdTask.csv_file` is really source metadata for an external input dataset.
Both should migrate in that direction while preserving backward compatibility.

`ProdConfig` should carry workflow-mode defaults: external EVGEN input, internal
EVGEN stage execution, stage template, transformation or executable, splitting
strategy, resources, and site/queue defaults. Task-level `overrides` remain the
last-mile specialization mechanism for a reusable production config.

## Workflow Modes

The same production intent should be expressible in two modes:

```text
external EVGEN dataset -> simulation/reconstruction -> output dataset(s)
```

and later:

```text
internal EVGEN stage -> simulation stage -> reconstruction stage -> output dataset(s)
```

The internal EVGEN case should still be one PCS production task or request,
with an internal workflow graph describing the stages. It should not require a
graph of multiple top-level PCS tasks. The model should also allow both modes
to be run and compared under the same physics, EvGen, simulation, and
reconstruction metadata.

## Lifecycle

The public catalogue publication state is not production readiness. Sakib's
current issue fields are sufficient to create a public planning row and a
partial PCS request, but not a fully specified PCS production task.

The PCS lifecycle is the simple five-state set already on `ProdTask`:

```text
draft  →  ready  →  submitted  →  completed | failed
```

`draft` covers every incomplete state — missing metadata, missing tag
mapping, unvalidated inputs, partial public-catalogue projection. When
everything required is in place and the operator has confirmed, the task
transitions to `ready`. From `ready` the operator submits, which
transitions to `submitted`; PanDA then drives the terminal transitions
to `completed` or `failed`.

PCS infers likely values where possible, surfaces missing fields and
validation errors via the same surface (web / REST / MCP), and supports
operator completion through templates and defaults. The visible state
stays `draft` until readiness checks pass and the operator confirms;
there is no separate `needs_metadata`, `planned`, or
`ready_for_operator_review` state — those are sub-states *inside*
`draft` driven by validation, not enumerated transitions.

Readiness checks include path / CSV manifest validity, file readability
where possible, event counts, tag mapping, production config, and
public catalogue projection.

PCS should store the public catalogue mapping internally:

```text
public_catalog_repo = eic/epic-prod
public_catalog_issue
public_catalog_pr
public_catalog_row_index
public_catalog_csv_path = docs/_data/datasets.csv
public_catalog_row_key = Issue=<issue number>
public_catalog_page_url
public_catalog_commit_sha
```

The GitHub issue number is the durable update key. The visible row index is a
useful human locator, but advisory.

## Intake Surface

All intake — from bots (via MCP), scripts, and the web UI — goes through
the same service layer (`pcs.services`). REST endpoints and MCP tools are
peer adapters over the same service functions; the contract (validation,
idempotency, lifecycle rules) is identical on both surfaces.

| Method | Endpoint | Purpose |
|---|---|---|
| POST   | `/pcs/api/datasets/`                          | Generic create. Body carries `metadata` (validated for external source.kind/location). |
| POST   | `/pcs/api/datasets/intake/`                   | Idempotent: given a CSV-manifest location (+ optional tag handles), find-or-create the external EVGEN Dataset, return its DID. |
| POST   | `/pcs/api/prod-tasks/`                        | Generic create. |
| POST   | `/pcs/api/prod-tasks/intake/`                 | Idempotent on a request key (e.g. `epic-prod#<issue>` or `csv_path+row_key`): create a draft ProdTask, ensure linked input Dataset(s), persist `public_catalog_*` mapping fields in `overrides`, return the task. |
| POST   | `/pcs/api/prod-tasks/<name>/link-input/`      | Link an existing Dataset as input by DID (writes `overrides.input_dataset_did(s)`). Sugar over PATCH. |
| POST   | `/pcs/api/prod-tasks/<name>/set-status/`      | Lifecycle transition with rule enforcement (e.g. only `ready → submitted`). |
| POST   | `/pcs/api/prod-tasks/record-submission/?name=`| Record JEDI submission outcome (`panda_task_id`, `status='submitted'`). Rejects if `panda_task_id` already set, or `status != 'ready'`. |
| GET    | `/pcs/api/prod-tasks/command/?name=&fmt=`     | Submission artifact — `condor`/`panda`/`jedi`/`dump`. |
| GET    | `/pcs/api/{datasets,prod-tasks}/`             | List with filters (`stage`, `source_kind`, `status`, `public_catalog_issue`, …). |

### Idempotency keys

The two `intake/` endpoints are idempotent and require a stable key in
the request body:

- `datasets/intake/` keys on `source.location` (+ `source.kind`, default
  `csv_manifest`). Repeated calls with the same location return the same
  Dataset row.
- `prod-tasks/intake/` keys on either:
  - `public_catalog_issue` when the request originated from a GitHub
    issue, or
  - `(public_catalog_csv_path, public_catalog_row_key)` when the request
    is identified by a row in `datasets.csv`.

Repeated calls with the same key return the same ProdTask row, never
duplicate. New input fields are merged into the existing draft (until
the task is locked or submitted).

### MCP tools

Each MCP tool is a peer to the corresponding REST endpoint, calling the
same service function. The two surfaces stay aligned because they share
business logic.

Read:
- `pcs_dataset_list(stage=None, source_kind=None, source_location=None, scope=None, name_contains=None, limit=20, offset=0)`
- `pcs_dataset_get(did=None, dataset_name=None)`
- `pcs_prodtask_list(status=None, public_catalog_issue=None, name_contains=None, limit=20, offset=0)`
- `pcs_prodtask_get(name)`
- `pcs_prodtask_artifact(name, fmt='dump')` — `condor` / `panda` / `jedi` / `dump`

Write (intake / lifecycle):
- `pcs_dataset_intake(source_location, source_kind='csv_manifest', physics_tag=…, evgen_tag=…, simu_tag=…, reco_tag=…, detector_version=…, detector_config=…, scope='group.EIC.evgen', stage='evgen', description='', created_by=…)` — idempotent on `(source_kind, source_location)`.
- `pcs_prodtask_intake(public_catalog_issue=…, public_catalog_csv_path=…, public_catalog_row_key=…, name=…, dataset=…, prod_config=…, description=…, input_dataset_did=…, public_catalog_*=…)` — idempotent on the catalogue key.
- `pcs_prodtask_link_input(task_name, did=None, dids=None)`
- `pcs_prodtask_set_status(task_name, status)`

Submission itself (`pcs-task-cmd <name> --submit`, which calls
`pandaclient.Client.insertTaskParams()`) is **not** exposed via MCP.
The MCP server runs on swf-monitor and has no operator PanDA auth
context; the operator runs the CLI on a host where their proxy or
OIDC token is live. A future `pcs_prodtask_submit` MCP tool needs an
OIDC service account on swf-monitor first.

### Public catalogue mapping fields

Stored in `ProdTask.overrides` under reserved keys (no schema columns
needed for the interim model):

`public_catalog_repo`, `public_catalog_issue`, `public_catalog_pr`,
`public_catalog_row_index`, `public_catalog_csv_path`,
`public_catalog_row_key`, `public_catalog_page_url`,
`public_catalog_commit_sha`.

## Request Composer

The request composer (`/pcs/request/`) is the native intake surface on the
path to replacing the Google Form, which serves as its guide rather than its
specification. A requester describes the physics in plain terms — process,
beams, species, Q², generator, sample variant, event count, PWG and/or DSC
(distinct fields, each optional), intended use, optional input location and
configuration repository — and the
page shows, live, the physics configurations the system already has that
match: what they are, where they were produced, and how much. Contact name
and email are required, prefilled for signed-in users. "Use this"
bases the request on an existing configuration, recording the same
physics-configuration anchor the CSV import writes, so a composed request
projects onto campaign editions exactly like an imported one. The mapping is
deterministic throughout: form fields land in the request's filter block and
columns; no inference intervenes.

Submission is a `/pcs/api/` POST (external-safe through the swf-remote
proxy) gated by login; the page itself is readable by anyone, with the
submit control visible but inactive for visitors. Each submission is an
action-stream event.

The composer is also the first "my epicprod" surface: the signed-in user's
past requests are shown as the starting point for the next one ("start from
this"), and their working group and contact are remembered in per-user
preferences. The same identity surface is where user-level analysis support
enters later — the system that knows a user's physics and runs their bulk
production is positioned to run their analysis over it as well.

## Dynamic Public Catalog

The present GitHub/Jekyll public catalog webpage provides a static snapshot of production requests, but does not offer dynamic modification: cloning of established requests into new campaigns, modification of requests and injection into campaigns, adjustment of priorities, withdrawal of requests, and other changes, all of them based on authenticated users with particular production system rights.

PCS already has dynamic listings and edit/copy/delete/update extensible functionality in its web interface, including the task interface. PCS dynamic changes should still preserve traceability/audit/review, perhaps by GitHub PRs at first and later by PCS-native audit logs and approval workflows. This dynamic interface will be developed as a candidate for adoption as the official public catalog interface, once PCS and automated production is proven and established on an ePIC owned server. 

## Roles and Approval

Once PCS is integrated with the ePIC phonebook and COmanage, role assertions gate the dynamic catalog. PWG members author Physics Configs within templated requirements enforced by PCS. Production managers approve those configurations before they propagate to automated production.

## Implementation Plan

1. Extend `Dataset` with stage and source metadata.
2. Add task-dataset relations for input, output, and intermediate dataset lists.
3. Migrate current `ProdTask.dataset` semantics to an output dataset relation.
4. Move current `ProdTask.csv_file` semantics to external input dataset source
   metadata, with backward compatibility during transition.
5. Add workflow-mode/template fields to `ProdConfig`.
6. Add the intake endpoints listed under "Intake Surface"
   (`datasets/intake/`, `prod-tasks/intake/`, `link-input/`, the
   lifecycle gates on `set-status/` and `record-submission/`).
   Expose each as a peer MCP tool calling the same service function,
   so DISpatcher and other MCP clients drive intake, status transitions,
   comment append, catalogue-row preview, and PCS-driven GitHub issue
   creation through the shared service layer rather than constructing
   REST queries.
7. Continue to develop the integrated automated production workflow from user input to
   running task, including a dynamic web interface providing documentation and
   flexible interaction with and control of the automated production system,
   as well as the mattermost/bot interface.
