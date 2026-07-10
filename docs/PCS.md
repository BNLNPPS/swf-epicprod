# PCS — Physics Configuration System

PCS is the Physics Configuration System within **epicprod**, the ePIC automated
production system. PCS manages the configuration and campaign records that
epicprod uses to submit, monitor, retry, and account for production work through
PanDA and Rucio.

PCS is one subsystem, not the whole production system. The broader epicprod
system includes the task catalog, PanDA monitor views, production operations
agent, Rucio lineage/update flows, payload-log retrieval, alarms, system-status
checks, and external access through the production web face. PCS supplies the
structured physics/configuration layer for that system: tags, datasets,
production configs, production tasks, and request/catalog linkage. The
system-level description of the whole WFMS, above these implementation docs,
is <https://epic-wfms-docs.readthedocs.io>.

**URL:** `/swf-monitor/pcs/`

## What It Does

Production configuration is organized as **tags** — named parameter sets that capture the settings for each stage of the simulation pipeline:

| Tag Type | Prefix | What It Captures | Example |
|----------|--------|-----------------|---------|
| **Physics** | p | Process, beam energies, species, Q2 range | p2004 — DIS 5x41 |
| **EvGen** | e | Generator name and version, backgrounds | e1 — pythia8 8.310 |
| **Simulation** | s | Detector sim version, background config | s1 — npsim 26.02.0 |
| **Reconstruction** | r | Reco version, calibration, alignment | r1 — eicrecon 26.02.0 |

Physics tags are grouped by **category**, reflected in their numbering: Single Particle tags are p1xxx, DIS are p2xxx, DVCS are p3xxx, SIDIS are p4xxx, Exclusive are p5xxx, and Background are p6xxx.

The system is seeded with configurations from the [26.02.0 production campaign](https://eic.github.io/epic-prod/FULL/26.02.0/): physics, evgen, simu, and reco tags derived from that campaign's datasets. The live tag counts grow as new configurations are added.

A fifth tag type, **background** (`k`), captures a named, versioned background configuration (beam-gas, synchrotron radiation, or overlay samples) independent of any physics signal. It is implemented and optionally composed into a dataset — see [Background Tag](PCS_BACKGROUND_TAG.md).

## Tag Lifecycle

```
draft  ──►  locked
```

- **Draft** — editable. You can modify parameters, copy from other tags, or delete.
- **Locked** — immutable. One-way transition. Ensures reproducibility: once a tag is used in production, its meaning never changes.

Only the tag creator can edit, lock, or delete their own drafts. Anyone can copy any tag to create their own variant.

During alpha commissioning all tags remain draft and the lock requirement for datasets and submission is lifted; see [Commissioning Relaxations](COMMISSIONING_RELAXATIONS.md) for the current relaxation and how it is re-tightened.

## Using the Tag Panel

The main interface is the **panel view** — a split-pane layout with a tag browser on the left and detail/edit panel on the right. Access it from the PCS dropdown in the nav bar (Physics Tags, EvGen Tags, Simu Tags, Reco Tags).

### Browsing

- **Arrow keys** navigate the tag list (keyboard focus is on the list at page load)
- **Search box** filters by any text in tag name, description, category, or parameter values
- **Status pills** filter by draft/locked
- **Creator pills** filter by who created the tag (appears when multiple creators exist)
- **Parameter dropdowns** filter by distinct values of each parameter (e.g. filter physics tags to only `beam_species=eAu`)
- **Clear** button appears when any filter is active; resets everything

### Viewing

Click any tag to see its full detail on the right: description, creator, and all parameters.

### Creating

Click **New Tag** to enter create mode. The form shows required fields (marked with *) and optional fields. For fields with known values, a dropdown offers choices; select "Other..." to enter a free value.

While creating, click any existing tag in the list — its values fill in as suggestions (grey italic). Fields you've already edited show a yellow suggestion bar instead of overwriting, so you can compose a new tag from pieces of existing ones.

The title shows the predicted next tag number (e.g. "New Tag p1020"). The actual number is assigned on save.

### Editing

Your own draft tags show an **Edit** button. Editing works in the same panel — no separate page. Changed fields appear in dark green. The Save button stays disabled until you actually change something, and re-disables if you revert all changes.

While editing, click other tags to see suggestion bars for differing values. Click "Apply" to adopt a suggested value.

### Copying

Any tag (yours or others') has a **Copy** button. This fills the create form with all values from the source tag, sets you as creator, and lets you modify before saving. Useful for creating variants of existing configurations.

### Locking and Deleting

Your own drafts show **Lock** and **Delete** buttons. Locking is permanent — confirm carefully. Deletion removes the tag.

## Tag Numbering

Tag numbers are auto-assigned. You never pick a number manually.

- **Physics tags**: `category digit * 1000 + global suffix`. The suffix increments globally across all categories, so numbers within a category may have gaps (e.g. p2001, p2005, p2020). This is expected.
- **EvGen/Simu/Reco tags**: Simple sequential increment (e1, e2, ... / s1, s2, ... / r1, r2, ...).

All counters are managed atomically via PersistentState to prevent conflicts.

## Parameter Schemas

Each tag type has required and optional parameters defined in `pcs/schemas.py`. Adding a field there makes it appear in forms and validation — no database migration needed.

**Physics (p):**
- Required: `process`, `beam_energy_electron`, `beam_energy_hadron`
- Optional: `beam_species`, `q2_range`, `decay_mode`, `hadron_charge`, `coherence`, `model`, `polarization`, `notes`

**EvGen (e):**
- Required: `generator`, `generator_version`
- Optional: `signal_freq`, `signal_status`, `bg_tag_prefix`, `bg_files`, `notes`

**Background (k):**
- Required: `background_type`
- Optional: `bg_source`, `bg_mechanism`, `bg_generator`, `beam_energy_electron`,
  `beam_energy_hadron`, `beam_species`, `cross_section`, `signal_freq`,
  `signal_status`, `bg_tag_prefix`, `bg_files`, `evtgen_file`, `notes`

**Simulation (s):**
- Required: `detector_sim`, `sim_version`
- Optional: `background_config`, `digitization`, `notes`

**Reconstruction (r):**
- Required: `reco_version`, `reco_config`
- Optional: `calibration_tag`, `alignment_tag`, `notes`

## REST API

Base URL: `/swf-monitor/pcs/api/`

Tags support list, create, get, update (draft only), and lock. Replace `{type}` with `physics-tags`, `evgen-tags`, `simu-tags`, or `reco-tags`.

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/{type}/` | List tags |
| POST | `/{type}/` | Create tag (number auto-assigned) |
| GET | `/{type}/{number}/` | Tag detail |
| PATCH | `/{type}/{number}/` | Update draft tag |
| POST | `/{type}/{number}/lock/` | Lock tag (permanent) |

Physics tag creation requires a `category` field (digit). Tag numbers are always auto-assigned.

### Production Tasks — submission artifacts

A single read-only endpoint regenerates a task's submission artifact from current PCS state on every call (no DB writes):

```
GET /swf-monitor/pcs/api/prod-tasks/command/?name=<task_name>&fmt=<format>
```

| `fmt` | Content-Type | Contents |
|-------|--------------|----------|
| `condor` | `text/plain` | env-prefixed `submit_csv.sh` command |
| `panda` | `text/plain` | `prun` command |
| `jedi` | `application/json` | `taskParamMap` for `Client.insertTaskParams()` |
| `dump` | `application/json` | Full view: task + dataset + all four tags + prod config + effective config |

The parameter is named `fmt` because DRF reserves `format` for its own content-negotiation.

### `pcs-task-cmd` — the CLI wrapper

`scripts/pcs-task-cmd` is a stdlib-only Python client over the endpoint above. It is the recommended way for production operators and automation scripts to fetch submission artifacts — no Django import, no DB credentials.

```bash
pcs-task-cmd <task_name> --format {condor|panda|jedi|dump}
```

Environment:

| Variable | Default | Purpose |
|----------|---------|---------|
| `SWFMON_URL` | `https://epic-devcloud.org/prod` | swf-monitor base URL |
| `SWFMON_TOKEN` | *(unset)* | Optional DRF token for non-public deployments |

Examples:

```bash
# Inspect everything about a task
pcs-task-cmd group.EIC.26.02.0.epic_craterlake.p3001.e1.s1.r1 --format dump

# Submit to JEDI (requires a valid PanDA auth context: proxy or OIDC token)
pcs-task-cmd <name> --format jedi | python -c '
import json, sys
from pandaclient import Client
print(Client.insertTaskParams(json.load(sys.stdin)))
'

# Inspect what would go to PanDA prun
pcs-task-cmd <name> --format panda

# Get the env-prefixed Condor command (pipe into bash)
eval "$(pcs-task-cmd <name> --format condor)"
```

## Datasets

A dataset is the concrete production unit: one sample, produced by a single task and registered as a single Rucio dataset. Its identity composes the classification tags with any sample-variant discriminators into one name, which serves as both the produced Rucio dataset name and the PanDA task name (`outDS`).

```
{scope}.{detector_version}.{detector_config}.{physics_tag}.{evgen_tag}.{simu_tag}.{reco_tag}[.{background_tag}][.{sample_name}]
```

Examples:

```
group.EIC.26.02.0.epic_craterlake.p3001.e1.s1.r1
group.EIC.26.02.0.epic_craterlake.p1141.e37.s1.r1.45to135deg
```

The version segment is the detector version: it describes the conditions of the produced data. Campaign membership is bookkeeping for production operations and does not rename the dataset identity. Reproducibility locking of the composed tags is enforced at submission prep, not at composition; during alpha that requirement is relaxed (see [Commissioning Relaxations](COMMISSIONING_RELAXATIONS.md)).

Because the version segment is part of the identity, each campaign's realization of a sample is its own dataset row — its **edition**. The campaign-invariant identity behind the editions is the **physics configuration**: physics tag + evgen tag (+ background tag) + sample variant — the thing the system is named for, what a requester asks for, and what a disposition decides about ("family" in earlier notes; distinct from a production config, the execution workflow settings). The simu and reco tags are not part of it: they are campaign-supplied software, bound per edition along with the detector config and version segment. A generator version bump deliberately breaks the physics configuration — that is a replacement, recorded with `replaced_by`. The physics configuration needs no entity of its own. Two dataset fields plan its future across campaigns:

- `propagation` — this edition's disposition, consumed at next-campaign creation: `continue` (default; mints the successor edition), `hold` (stays in the catalog, no next-campaign production), `final` (produced this campaign, then the configuration ends). Operators flip states, sometimes by approving an AI proposal; ingest never does.
- `replaced_by` — composed-name reference to the successor configuration when a retirement has a designated replacement (campaign-level changes such as an energy migration). A name reference, not a foreign key: the successor may not be materialized yet.

A physics configuration retired in campaign N is one whose N edition is `final`; one new in N has no N−1 edition. The campaign-over-campaign delta (continued / held / retired / replaced / new) is computed from these fields and feeds the campaign narrative and generated summaries ([EPICPROD_NARRATIVES.md](EPICPROD_NARRATIVES.md)).

Campaign lifecycle labels (`current`, `last`, `past`, `future`) are stored, singular, and human-owned: `current` is one campaign — the working catalog. **Producing** is derived, never stored: a campaign with Rucio arrivals inside `campaign_producing_window_days` (SysConfig, default 3) gets its own catalog tab ("26.06.0 · producing") for as long as data flows, whatever slot it occupies. The producing tab carries the one-click rotation — "Make <campaign> current" — which atomically promotes it while the incumbent current becomes last and the incumbent last becomes past (`campaign_promoted` event). Detection is automatic; the transition is always a human click.

Every disposition change records why. `dataset_propagation_set` (`pcs.services`; REST `POST /pcs/api/datasets/propagation/`) requires a free-text **comment** — the reason and its source together ("10×130 replaced by 9×130 per PSC meeting 2026-07-08") — and appends an entry (state, previous, comment, changed_by, changed_at; when the successor reference is touched, `replaced_by` — the new value, empty on an explicit clear — and `previous_replaced_by` when it changed) to the identity's append-only `metadata['propagation']['history']`; the column mirrors the newest entry, and every flip is unwindable from its own history entry. The successor reference is set with `replaced_by`, left untouched when empty, and blanked only by the explicit `clear_replaced_by` flag. Disposition proposal batches are named `<next-campaign>-dispositions-<YYYYMMDD>`, with `<next-campaign>` the full three-segment version (`26.07.0`, never `26.07`) — the batch name is where the successor campaign's name enters the system, becoming the campaign row and the version segment of every edition it mints; the catalog's Future tab derives the likely next campaign from pending batches so the coming transition is visible before the campaign row exists. One call covers a single dataset or a bulk selection and logs exactly one `dataset_propagation_set` action-stream event carrying the changed count, the comment, and the selecting filter — never one event per dataset. The catalog's bulk bar drives it: filter to the target set, select all, choose the state, enter the comment (prefilled with the user's last, remembered per user). Rows with a non-`continue` disposition carry a badge in the catalog list.

The Rucio DID adds the scope prefix and a block suffix: `group.EIC:...r1.45to135deg.b1`. Block `.b1` is always present. Rucio limits a dataset to 100k files; PCS subdivides into blocks (`.b1`, `.b2`, …) automatically as needed. The logical task name is the dataset name without the `.bN` suffix.

The detector-version identity and the sample variants below are extensions to the dataset model, defined here.

### Composed-name Suffixes

PCS separates the logical composed name from dynamic physical suffixes. The logical name is the stable PCS dataset/task identity. Physical PanDA and Rucio names may append suffixes so repeated submissions and Rucio block subdivisions have unique names.

Known dynamic tokens:

- `kN`: optional background-tag segment. It appears immediately after the `.pN.eN.sN.rN` tag run and is part of the logical PCS identity.
- `.tryN`: terminal PanDA/JEDI attempt suffix. This is the physical-attempt naming feature that lets one logical campaign task produce multiple concrete PanDA tasks without output-name collision. Attempt 1 has no suffix; attempt 2 is `.try2`.
- `.bN`: terminal Rucio block suffix. It is not part of the logical PCS identity or PanDA task name.

The canonical physical order is:

```
logical[.tryN][.bN]
```

Examples:

```
logical             -> logical PCS identity, first PanDA attempt
logical.b1          -> block 1 of the first attempt
logical.try2        -> second PanDA attempt
logical.try2.b1     -> block 1 of the second attempt
```

Interpretation strips registered terminal suffixes from right to left. Thus `logical.b1` and `logical.try2.b1` both resolve back to logical identity `logical`, with parsed block/attempt metadata. The implementation lives in `src/pcs/name_tokens.py`; new dynamic suffixes belong there first, then in this section, so name parsing does not fragment across services.

### Sample Variants

The tags carry the physics. They do not distinguish samples that share a physics configuration but are produced as separate datasets. Single-particle production illustrates this: a particle and energy define the physics tag (`particle`, `gun_energy`), while each angular range (`3to50deg`, `45to135deg`, `130to177deg`) is generated as its own dataset and task. The angular range is a production discriminator, not a physics parameter, so it is not a tag.

A sample variant attaches such a discriminator to a tag composition at the dataset level. A variant is a `(name, submit_params)` pair: a short name that becomes the trailing component of the dataset name, and the submission parameters that produce that sample (for an angular range, the gun angle bounds). Each variant materializes as a distinct dataset and task; the variant name is the only segment distinguishing them. The name is the literal discriminator value, `45to135deg`. On catalog import it is taken from the source path; in the panel a user enters it.

A tag composition that defines a variant list materializes as its variants, and the bare-tag dataset is then a template rather than a produced unit. A composition with no variants is itself the produced dataset; most compositions have none.

Generality is bounded: a variant is a single named sample with its parameters, not a set of independent dimensions the system multiplies out. Two production axes are expressed by naming each resulting sample, not by a composable key-and-value syntax in the name.

**Parsing.** The name is read positionally, not by splitting on `.`. The tag run `.p<n>.e<n>.s<n>.r<n>` occurs once and anchors the name; a `.k<n>` segment follows when present; the remainder is the sample name, taken verbatim and permitted to contain periods (`ma_0.1`). Registered terminal suffixes such as `.try<n>` and `.b<n>` are stripped first by the central suffix utility. Two reserved-token rules keep the parse unambiguous: a sample name's first segment must not match `k<n>` (the background tag) and its last segment must not match a terminal suffix token such as `try<n>` or `b<n>`. Discriminator values satisfy both.

Within one tag composition a sample name is unique. Identity, the duplicate check, and the completion unit all key on the tags together with the sample name; that unit is what completes and triggers validation (see [Validation](EPICPROD_VALIDATION.md)).

### External EVGEN Inputs

As an interim production-planning capability, PCS can represent externally
supplied generator-level inputs using `Dataset.metadata` rather than new schema
fields. This is intended for the current mode where PWGs or DSCs provide EVGEN
files or CSV manifests that PCS records but does not independently produce.

Example metadata:

```json
{
  "stage": "evgen",
  "source": {
    "kind": "csv_manifest",
    "location": "path/to/input.csv"
  }
}
```

The dataset API exposes convenience fields derived from this metadata:
`stage`, `external`, `source_kind`, and `source_location`. The full `metadata`
object remains the writable transport for this interim model.

## Production Configs

A production config is a reusable template capturing everything needed to build a submit command beyond what tags and datasets define.

**Dedicated fields** (DB columns):
- **Background mixing**: Enable/disable, cross section, EvtGen file
- **Output control**: Which output files to copy (reco, full, log), Rucio usage
- **Software stack**: JUG_XL tag, container image
- **Resource targets**: Target walltime per job, events per task
- **Condor template**: HTCondor submission template text
- **PanDA overrides**: Site, queue, working group, resource type
- **Rucio overrides**: RSE, replication rules

**Submission parameters** (JSON `data` field, extensible without migrations):

| Key | Example | Purpose |
|-----|---------|---------|
| `workflow_mode` | `external_evgen` | Production workflow mode: `external_evgen` (default; payload consumes a CSV-manifest input) or `internal_evgen` (payload runs evgen + sim + reco internally). Surfaced as `ProdConfig.workflow_mode`. |
| `submission_path` | `condor` | Submission path for tasks built on this config: `condor` (default; `submit_csv.sh` via `condor_submit`) or `panda` (JEDI `taskParamMap` via `Client.insertTaskParams`). Surfaced as `ProdConfig.submission_path`. Independent of `workflow_mode` (whether evgen runs in-house or is read from input files). See `EPICPROD_TASK_CATALOG.md` §2. |
| `transformation` | `runGen-00-00-02` | PanDA TRF script name/version |
| `processing_type` | `epicproduction` | PanDA classification |
| `prod_source_label` | `managed` | PanDA authorization (managed/test) |
| `vo` | `wlcg` | Virtual organization |
| `n_jobs` | `1000` | Jobs per task submission |
| `events_per_job` | `100` | Events per individual job; stored in `ProdConfig.data` and surfaced as `Events/Job` |
| `events_per_file` | `1000` | Events per output file |
| `files_per_job` | `1` | Output files per job |
| `corecount` | `1` | Cores per job |
| `no_build` | `true` | Skip PanDA build step |
| `skip_scout` | `true` | Skip PanDA scout jobs. Surfaced in the Prod Config UI as the positive **Scout Mode** toggle; unchecked writes `skip_scout=true`. New configs use the last saved toggle state from the user's database-backed JSON preferences; with no remembered value, scout mode defaults off. |
| `exec_command` | `./run.sh` | Payload command (--exec) |
| `log_rse` | `EIC-XRD-LOG` | Log-output RSE passed to the payload as `LOG_RSE`; stored in `ProdConfig.data` and surfaced as `Log RSE` |
| `scope` | `group.EIC` | Rucio scope for submission |

Production configs are always mutable — they are working templates. The PanDA task/job spec is the immutable record of what actually ran.

## External Access

PCS pages and REST endpoints reach external users via the swf-remote
proxy at `epic-devcloud.org`. All `pcs/` pages and the `pcs/api/`
REST surface are proxied wholesale by catch-all routes in
`swf-remote/src/remote_app/urls.py`, so a new PCS URL needs no
swf-remote change; explicit named entries are needed only outside the
proxied trees or where swf-remote resolves the route by `{% url %}`
name. See [External Access](EXTERNAL_ACCESS.md) for the contract,
including the write-action trigger rules.

## JEDI Integration

PCS is being extended to submit tasks directly to JEDI (PanDA's Job Execution and Definition Interface) via the PanDA Python API, replacing the current approach of generating `prun` CLI commands as text. See:

- [JEDI Integration Design](JEDI_INTEGRATION.md) — architecture, field mapping, implementation plan
- [JEDI ePIC Proposal](JEDI_EPIC_PROPOSAL.md) — technical proposal for PanDA team review
- [Dataset Request Workflow](PCS_DATASET_REQUEST_WORKFLOW.md) — PCS-centered plan for Mattermost/DISpatcher dataset request intake, external datasets, public catalogue projection, and future EVGEN workflow stages

## MCP Tools

MCP tools for AI-assisted tag browsing and lookup:

| Tool | Description |
|------|-------------|
| `pcs_list_tags(tag_type)` | List tags with label, description, status, key params |
| `pcs_get_tag(tag_label)` | Full tag detail with all parameters |
| `pcs_search_tags(query, tag_type)` | Full-text search across tag labels, descriptions, and parameter values |

Tag creation, lock/delete, and dataset/prod-config management go through the REST API and the web UI — see the sections above. The submission-artifact endpoint (`/prod-tasks/command/`) plus the `pcs-task-cmd` CLI are the programmatic path for production operators.

The dataset and production-task MCP tools (`pcs_dataset_*`, `pcs_prodtask_*`) identify a dataset or task by its composed tag name — the same canonical identity used in URLs and REST (see [Datasets](#datasets)) — return it as `composed_name`, and accept it as the lookup key; legacy names remain resolvable.
