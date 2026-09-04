# PC Ingest

PC ingest turns the production team's legacy submission lines into
physics configurations (PCs) in PCS. A legacy line is the env-prefixed
`submit_csv.sh` call (eic/job_submission_condor) that produced a sample
before PCS:

```
OUT_RSE=BNL-XRD LOG_RSE=EIC-XRD-LOG PANDA_MAX_ATTEMPT=3 PANDATE=BNL_OSG_EPIC_PROD_1
PANDA_DISK=4096 PANDA_MEMORY=4096 PANDA_SKIP_SCOUT=true PANDA_WALLTIME=4
EBEAM=9 PBEAM=100_Au197 DETECTOR_VERSION=26.07.1 DETECTOR_CONFIG=epic_craterlake
JUG_XL_TAG=26.07.1-stable COPYRECO=true COPYFULL=false COPYLOG=false USERUCIO=true
X509_USER_PROXY=secrets/x509_user_proxy
scripts/submit_csv.sh osg_csv hepmc3 DIS/eAu/9x100/BeAGLE1.03.02-3.1_DIS_eAu_9x100_q2_1to10.csv 2
```

The environment carries the campaign settings (detector version and
config, container tag) and one operator's per-job settings (PanDA
queue and resources, attempts, scout skip, output RSEs, copy flags).
The positional arguments are the environment template, the input type,
the dataset definition's CSV path in eic/simulation_campaign_datasets,
and the target hours per job. The CSV path names the definition the
nightly definitions sweep already holds (EPICPROD_EVGEN_INPUTS.md
§ The Definitions Side), which is the physics identity of the line.

## The page

`/pcs/ingest/` (PCS menu, "PC ingest") is one large text box. Any
number of lines are pasted and analyzed; each becomes one row, in
order, carrying:

- **State** — identified, new, near miss, unresolved, or unparsed
  (below).
- **Physics** — the derived physics parameters (process, beams,
  species, Q² range, and any further axis), with the EVGEN path the
  identity was derived from.
- **Generator** — generator, version, and radiation as derived.
- **Sample** — the sample variant, when the path carries one.
- **Campaign settings** — the detector version with its campaign
  family, the detector config, the container tag, and the per-job
  settings as previously submitted. These are shown, not acted on.
- **Events** — the definition's measured event count and file count
  from the definitions inventory, when the definition is costed.
- **Family** — the physics configurations sharing the line's process,
  beams, species, and generator name, each with what distinguishes it
  (Q² range, generator version, sample), linked to its PC page. A new
  line is thereby placed: "new; 3 defined in this family".
- **Result** — the identified PC and whether it has an edition in the
  line's campaign, or the reason for any other state.

Analysis is read-only and open to anyone. Accepting requires a
signed-in user: an Accept button per new row, "Accept as new" per
near-miss row, and one "Accept all new" for the new rows together.
Near misses, unresolved, and unparsed rows are never accepted in bulk;
they stay in the table with their reason until addressed.

## States

| State | Meaning | Accept |
|---|---|---|
| identified | A physics configuration with this identity exists. The row names it and its edition in the line's campaign, if any. | refused |
| new | No configuration has this identity. | per row, or all at once |
| near miss | The physics tag exists with configurations that differ only in generator version, radiation, background tag, or sample; each is named with the differing axis. | per row, after review |
| unresolved | The physics is derived but the generator is not (a merged background definition names no generator, a bare generator name carries no version). Left for manual association, never guessed. | refused |
| unparsed | Not a `submit_csv.sh` call, no CSV path, or no physics area recognized in the path. The reason is stated. | refused |

The configuration key carries the generator identity lowercased, as
`physics_config.evgen_identity` records it; the row displays the
generator as the path spells it.

## Identity derivation

The line's identity is derived exactly as the catalog import derives
it, so an ingested configuration and an imported one meet on the same
tags:

1. The CSV path is looked up in the dataset definitions inventory
   (the `dataset-definitions.json` snapshot). The EVGEN directory of
   the definition's first file, read from the local definitions
   clone, is the derivation path (`EVGEN/DIS/BeAGLE1.03.02-3.1/eAu/9x100/q2_1to10`);
   the inventory's lowercased tail is the fallback, and the CSV file
   name's tokens the fallback after that. A line whose definition is
   not in the inventory says so on its row.
2. `physics_match.derive_physics` scans the path into the physics
   parameters; `find_or_create_physics_tag` in dry-run resolves the
   tag or reports that one would be created. Backgrounds resolve to
   the signal-free physics tag plus a derived background (k) tag.
3. `physics_match.derive_evgen` resolves generator, version, and
   radiation from the path. A path that names no generator version
   (the pythia8 DIS paths, merged backgrounds) is looked up in the
   catalog: the datasets whose source location is this path carry the
   generator the CSV import resolved from the request's version
   column, and when they agree that identity is used and the row says
   so. An identity those editions carry only as a tag binding, never
   observed in a path (the default e1 binding on background editions),
   identifies an existing configuration but never mints a new one:
   such a line is unresolved rather than new. Disagreement or no
   record leaves the line unresolved. `single_particle_angle` gives
   the sample.
4. The physics-configuration key is composed by
   `physics_config.config_name` from the tag label, the generator
   identity, the background tag, and the sample, and matched against
   `PhysicsConfig.config_key`.

## Acceptance

A physics configuration exists only through an edition (PCS.md
§ Datasets: composition implies the configuration; nothing else creates
one). Accepting a line therefore composes the edition the line
describes for the campaign it names: the physics and generator tags
found or created, the background tag when one applies, the stage
sentinels s0 and r0 (EVGEN-stage material, as the CSV import records
requests), the detector version and config from the line, and the
sample. Saving the edition binds and, when needed, mints the PC.

The edition records its provenance in `metadata['ingest']`: the raw
line, its environment, the target hours and any further arguments, and
the definition's cost record; `metadata['source']` carries the EVGEN
path and the CSV path as a `csv_manifest` source, as the CSV import
does.

Acceptance re-derives every line server-side and refuses, with the
reason, a line that is identified, a near miss not explicitly allowed,
one that names no detector version and config, one whose campaign
family is not defined in PCS, or one whose composed edition already
exists. A stale page cannot create a duplicate. Each accept call is
one action-stream event (`pc_ingest_accept`) carrying the accepted
count and names.

The per-job settings on the line are not applied. Production
configuration is carried per campaign in PCS, not per task; the line's
values stay on the row and in the edition's provenance for comparison
when a request is created.

## Surfaces

## Creating the request

An identified row with an edition in the line's campaign, which an
accepted row is, carries a second action, "Create request". It records
what the CSV import records for a catalog row
(PCS_DATASET_REQUEST_WORKFLOW.md), in one transaction:

- **The request** (`ProdRequest`), anchored on the edition's composed
  name as every request is, with the definition's measured event count,
  the EVGEN path as the input location, the generator and version, the
  physics filters the request pages resolve by, and the line with its
  environment, target hours and definition cost under
  `data['ingest']`. Requestor and the triage fields are left to the
  production team. Idempotent on the CSV path (`source_row =
  ingest:<csv path>`).
- **The draft task** (`ProdTask`) in the campaign the line names, named
  by the EVGEN path, bound to the edition, with the request's fields
  applied and the line's per-job settings under `overrides['ingest']`
  for comparison against the campaign's configuration. Its production
  configuration is the placeholder until the production team binds the
  campaign's, as for CSV-imported tasks: the line's per-job settings are
  not a configuration. Readiness and submission happen on the task
  page; the ingest page submits nothing.

The action is refused, with the reason on the row, for a line that is
not identified (accept it first), one whose configuration already
carries a request (the row names the requests), a campaign that is not
defined, or a task name already in use. Each call is one action-stream
event (`pc_ingest_request`).

## Surfaces

| Surface | Path |
|---|---|
| Page | `/pcs/ingest/` |
| Analyze | `POST /pcs/api/ingest/analyze/`, body `{"text": "<lines>"}` → rows, counts, definitions stamp |
| Accept | `POST /pcs/api/ingest/accept/`, body `{"lines": ["<line>", ...], "allow_near_miss": false}` → per-line results; signed-in users only |
| Create request | `POST /pcs/api/ingest/request/`, body `{"lines": ["<line>", ...]}` → per-line results with `request_id` and `task_name`; signed-in users only |

The endpoints are JSON in and out on the `/pcs/api/` surface, so the
page works alike on the internal face and through the swf-remote
proxy (EXTERNAL_ACCESS.md). The implementation is `pcs/ingest.py`.
