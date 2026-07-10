# ePIC Production Data Lineage

A production system must record and reference the data it produces. epicprod
produces reconstruction datasets that land in Rucio; PCS already holds their full
production provenance — physics, EvGen, simulation, reconstruction tags,
campaign, requestor, use flags, status — with rich filtering over all of it. What
it does not yet hold is the **explicit Rucio reference** for each produced
dataset: the `epic:/RECO/…` DID(s), their RSEs, and file counts. Recording those
closes the production record — and, the provenance already being filterable, lets
the catalog locate produced data by any provenance dimension.

The immediate application is verifying campaign **lineage and completeness** —
the record the production team keeps today by hand (the `default_datasets`
catalogue and monthly completion email).

## Problem and scope

The requested-side information is already in the PCS task database —
`default_datasets` seeded our catalog and everything it shows is in our
`ProdTask` records. What is missing, in our catalog *and* on the
`default_datasets` HTML page, is the explicit link to Rucio. Without it a filter
cannot yet resolve to Rucio datasets or files; with it, it can.

The work is to **gather** the Rucio links and write them onto the existing task
records' extensible JSON — not a second database of mappings. Stored there, the
links unlock two capabilities over the catalog's existing filters:

- **Reference** — resolve a filtered task set to its produced Rucio
  datasets/files. A plain catalog read, no credential.
- **Access** — fetch the produced data itself over XRootD from those links:
  sample a file, pull a whole file when small (as the payload-log doer does for
  logs), examine it. The agent's credentialed xrootd substrate; no credential in
  the web tier.

So we win not just the correlation but **data access**.

Gathering is **transitional, and specific to pre-PanDA data**. The current
(Condor-based) production system does not make or preserve the production→Rucio
connection in flight, which is exactly why it must be reconstructed after the
fact — and why the production team's monthly completion emails exist. PanDA
records that connection in flight, so PanDA-produced data carries it from the
start. The sweep therefore backfills pre-PanDA campaigns only; reference and
access over the recorded links are permanent.

## Derivation (request filters → produced DID → replicas)

*(Implemented — `match_requests_to_rucio_snapshot`, `_filter_match`,
`import_jlab_rucio_current_snapshot`, `refresh_rucio_snapshots`, and
`_rucio_match_to_output` in `pcs/services.py`.)*

The implemented mechanism is a **filter-based match over a fetched Rucio
snapshot**, not a path-string glob. A campaign's full Rucio listing is fetched
once into a snapshot, then each `ProdTask` is matched to the produced datasets
in it by comparing semantic filter fields. Matching on path strings does not
work: a produced RECO DID carries extra segments (generator, radiation, charge)
and a different Q² spelling than the requested EVGEN path.

**Snapshot fetch — Rucio → JSON:**

- `import_jlab_rucio_current_snapshot` authenticates to JLab Rucio and calls
  `fetch_jlab_rucio_campaign` for both `/RECO/<campaign>` and `/FULL/<campaign>`.
  Each fetch does a `/dids/<scope>/dids/search` for `<campaign_path>/*`, then a
  per-dataset metadata + `/replicas/.../datasets` read (run in a thread pool to
  stay under the request timeout), yielding for each produced dataset its `did`,
  `length`, `bytes`, and per-RSE `rse_replicas`.
- The snapshot is written to one JSON file per campaign under the snapshot
  directory (`current-<campaign>.json`) and reused for the match.

**Match — request filters → produced DID(s):**

- `match_requests_to_rucio_snapshot` indexes the snapshot's datasets and extracts
  the filter axes of each produced DID. For every `ProdTask` in the campaign it
  reads the request filter block (the persisted `csv_import.filters`, or a fresh
  extract from the CSV input path) and compares the two via `_filter_match` on the
  shared semantic axes — detector, beam, physics, Q² overlap, and (for
  single-particle paths) species/energy — never on path strings.
- Each matching produced dataset is converted by `_rucio_match_to_output` into an
  `overrides.outputs` entry carrying `did`, `stage`, `version`, derived
  `filters`, per-RSE `rses` (`files`/`total`/`complete`), aggregate `file_count`,
  `bytes`, `complete`, and `checked_at`. Datasets in the snapshot that no request
  matched are stashed on `campaign.data['rucio_unmatched']` for the catalog to
  surface.

**Completeness — replicas, counts:**

- Per-RSE completeness comes from each replica's available-vs-total file count in
  the snapshot record; a dataset is `complete` only when every RSE replica is
  fully available.
- File count and byte size are taken as the max across RSE replicas (Rucio
  reports them per replica, identical across RSEs).

## PCS data model (write target)

The links go onto **`ProdTask.overrides`** (JSONField) under a reserved key — the
same interim convention that already holds `input_dataset_dids` and the
`public_catalog_*` fields. Relevant `ProdTask` fields:

- `input_source_location` (property; `Dataset.source_location`, i.e.
  `Dataset.metadata['source']['location']`, `csv_file` fallback) — the requested
  `/volatile/eic/EPIC/EVGEN/<suffix>` path the request filter fields are extracted
  from for the match.
- `campaign` → `Campaign` (FK) — selects the Rucio snapshot to match against.
- `dataset` → `Dataset` (FK) — the PCS *output* dataset. Its `did` is the
  PCS-composed `group.EIC:….b{N}` identifier and `detector_config` the detector.
  For **pre-PanDA Condor production** this is a different namespace from the
  produced `epic:/RECO/…` Rucio DID (hence the filter-based match below). PanDA
  production instead carries the composed identity *as* its Rucio DID (see
  Phase 4), so there the two coincide.
- `request` → `ProdRequest` (FK) — originating PWG/DSC request; carries
  `nevents` (the requested event count), intended for a future
  expected-vs-actual completeness check. The implemented completeness is per-RSE
  replica availability only.
- `overrides` — the interim JSON the links are written to.

`overrides.outputs` — a list, one entry per produced Rucio dataset
(lifecycle-neutral, never aggregated); the single home for the produced-output
↔ task association, read via the `ProdTask.outputs` accessor:

```json
{
  "outputs": [
    {
      "did": "epic:/RECO/26.04.1/epic_craterlake/<suffix>",
      "stage": "RECO",
      "version": "26.04.1",
      "filters": {"detector": "epic_craterlake", "beam": "10x100", "physics": "DIS", "q2": "", "species": "", "energy": ""},
      "rses": [{"rse": "BNL-XRD", "files": 1234, "total": 1234, "complete": true}],
      "file_count": 1234,
      "bytes": 1234567890,
      "complete": true,
      "checked_at": "<iso8601>"
    }
  ]
}
```

The same schema serves current and past campaigns — today's current is
tomorrow's past, with no reshape on transition. `migrate_outputs_schema()`
(standalone `scripts/migrate_outputs_schema.py`) folded the legacy `past_output`
block and the old `csv_import.output` rollup onto it, and the epic-prod
past-campaign ingest writes this schema directly (one bare-named campaign per
version, per-stage totals in `data['past_summary'][stage]`), so legacy-shaped
task overrides are no longer produced.

The past-campaign ingest itself is clockwork: a `catalog_sync` chain step
(`epic-prod-past-import.py`) pulls the eic/epic-prod bookkeeping clone —
the production team's nightly-regenerated, Rucio-derived record — and
re-runs the idempotent import, so every campaign's recorded production
content tracks what production publishes without operator action. The Past
tab's "Update from epic-prod" button remains as the on-demand override of
the same ingest, not the mechanism.

## Architecture

**Gather** *(implemented)* — standard credentialed-async pattern, no new
substrate: the catalog's **Update from Rucio** button POSTs to
`prod-tasks/rucio-snapshot-update/`, which publishes a `rucio_snapshot_update`
message to the prod-ops agent. The agent's `_handle_rucio_snapshot_update`
dispatches a `run_in_background` doer (`_do_rucio_snapshot_update`, holds the
proxy) that runs `refresh_rucio_snapshots` — fetch the JLab Rucio snapshot for the
current (and last) campaign(s) and rematch produced datasets onto each task's
`overrides.outputs`. On completion it publishes `rucio_snapshot_ready`, which the
catalog page receives over the SSE relay (`EventSource`) and refreshes live,
internally and through the swf-remote streaming proxy. The web tier holds no
credential.

- Trigger: the **Update from Rucio** button (on demand) and the nightly
  `catalog_sync` chain.
- Unit of work: the current and last campaigns — for each, fetch the snapshot
  once and match the campaign's `ProdTask` rows against it; the receiver thread
  never blocks.

**Arrivals sweep** *(implemented)* — clockwork detection of new files landing
in JLab Rucio, complementing the snapshot: where the snapshot is a deep fetch
of two campaigns, the sweep is a shallow query over all of them.
`sweep_rucio_arrivals` (`pcs/services.py`; the agent's
`rucio-arrivals-sweep.py` doer, a `catalog_sync` chain step) runs one
`created_after` DID query per root (`/RECO`, `/SIMU`) — the server-side
filter the eic/firehose notification action uses — windowed from the previous
sweep's timestamp so a missed night is covered by the next. New files are
grouped by campaign and location; each arriving campaign's row records
`campaign.data['arrivals']` (last arrival, counts by root, full location
breakdown) — the signal behind the campaign page's derived **producing**
status, whatever lifecycle slot the campaign occupies. One live
`rucio_arrivals` event carries the breakdown when anything arrived; arrivals
naming a campaign with no catalog row are reported in the event, never
dropped — an unknown arrival is the first signal of a new campaign
appearing.

**Firsthand reconciliation** *(implemented — `pcs/reconcile.py`)* — a
producing campaign's catalog records track Rucio directly, with no
dependency on the epic-prod bookkeeping cadence. After each snapshot fetch
of a producing campaign (outside current/last, whose outputs attach through
the request match and CSV heritage respectively),
`reconcile_campaign_from_rucio` applies three rules: a known DID
(``metadata.source.location``) refreshes its row's counts, volume, per-RSE
status, and the matching task outputs entry; an unknown DID resolves to a
physics configuration first — matching an existing edition, it joins that
identity as a physical sibling row and its outputs land on the edition's
task, never duplicating the edition; only a configuration with no edition
creates one, past-ingest style. Unresolved physics is reported, never
created. One `rucio_reconcile` event per campaign per run.

**Reference** *(future, building on the gathered links)* — a catalog read over the stored links, no credential: the existing
filter set (`EPICPROD_TASK_CATALOG.md` §7) collects the tasks' `outputs`
DIDs across the filtered set. Dataset-level reference is a pure read of the cached
links; file/PFN expansion is resolved **live against Rucio on demand** — file
lists are not cached, Rucio is their authority. Surfaced in the catalog's "Rucio
Monitor" feed (DID link, RSE badges, file count) per `EPICPROD_TASK_CATALOG.md`
§6, and as an action to list or export the produced datasets for a selection. A
**prod-navigation CLI** is a second front-end on this same filter→resolve REST,
covered by its own design doc (downstream of these links existing).

**Access** *(future)* — fetch the produced data over XRootD, the same credentialed substrate
as payload-log: a `run_in_background` doer constructs the per-RSE XRootD PFN
(replace the `epic:` DID prefix with the RSE prefix), `xrdcp`s a sample or a small
whole file under the proxy, caches it, and pushes the result to the browser on
`/topic/epictopic`. Sample and small-whole-file pull only; inspection follows the
payload-log model. Each is an individual handler.

## Proto-plan

**Phase 1 — gather.** *(Implemented.)* Fetch the campaign's Rucio snapshot once;
match each `ProdTask` to the produced datasets in it on the shared filter axes
(`_filter_match`); write `overrides.outputs`. Render in the catalog; push
`rucio_snapshot_ready` on completion. Validate by spot-checking a sample against
an independent manual Rucio query.

**Phase 2 — reference.** *(Future, building on the gathered links.)* Catalog
filter → the produced Rucio datasets, with on-demand file/PFN expansion (live,
uncached) — the system surfacing the data it produced from its provenance record.
The prod-navigation CLI is a second front-end on this REST.

**Phase 3 — access.** *(Future.)* Fetch produced data over XRootD from the stored
links — sample, small-whole-file pull, examine — reusing the payload-log xrootd
doer.

**Phase 4 — capture at source (PanDA data).** PanDA makes the production→Rucio
connection in flight, so for PanDA-produced tasks the output DID is recorded at
submission time rather than reconstructed by a sweep. Each physical submission is
recorded in `PandaTasks`; `ProdTask.panda_task_id` remains the current/preferred
pointer. The first attempt uses the PCS composed identity name itself
(`group.EIC:…`); retries and site races append `.tryN` so every concrete PanDA
task has a unique Rucio namespace. The sweep stays a backfill tool for pre-PanDA
campaigns, whose legacy DIDs PCS records and presents as found.
