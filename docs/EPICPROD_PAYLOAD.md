# The epicprod payload

## Principle

epicprod implements the in-job path end to end: the runner PanDA executes,
the payload it runs, the handling of what the payload produces, and
the registration of that output in the catalog of record. PanDA
carries the job. The campaign container supplies the software stack:
detector descriptions, npsim, eicrecon, ROOT, the background merger,
the Rucio client. The submission sandbox carries the runner and the
payload. The container image supplies no scripts the job runs.

The starting point is a clone of the production team's payload,
`run.sh` and its helpers from `eic/simulation_campaign_hepmc3`, taken
into this tree as it stands and evolved here rather than as patches
to the external repository: the payload is core epicprod function and
will change substantially, beginning with the registration step
(RUCIO_REGISTRATION_CONTRACT.md, RUCIO_RESILIENCE.md) and the payload
reporting continuous production depends on (CONTINUOUS_PRODUCTION.md
§ Payload metrics).

## The path today

The client-API EVGEN submission (JEDI_INTEGRATION.md § Client-API
EVGEN submission) ships a sandbox holding the manifest, an
`environment-<csv>.sh`, the JLab `eicprod` proxy, staged background
files, and the in-job dispatcher `evgen_job_dispatcher.py`. The
dispatcher reads its manifest row and hands it to
`/opt/campaigns/hepmc3/scripts/run.sh`, the payload checked out in
the container image. Everything from that point is the container's
copy of the production team's scripts:

| Stage | What run.sh does | Depends on |
|---|---|---|
| Environment | sources `environment*.sh` from the working directory by glob; prints host, site, disk, condor ads | the sandbox env file |
| Software | sources the detector setup for `DETECTOR_VERSION`; sets `RUCIO_CONFIG` to its own `rucio.cfg`, account `eicprod` | the container |
| Input | streams `hepmc3.tree.root` input from the JLab door, copies other inputs with `xrdcp` | `XRDRURL`, `XRDRBASE` |
| Background | merges signal and background with `SignalBackgroundMerger` from `BG_FILES`, rate-scaled skips and a seed mixed from the input name | the container, staged `BG_FILES` |
| Simulation | `npsim` under `prmon`, seeded per chunk | the container |
| Reconstruction | `eicrecon` under `prmon` | the container |
| Metadata | `parse_podio_metadata.py` reads geometry, beams and gun parameters from the FULL file; the software release from `eic-info` | PyROOT |
| Logs | tars the stage logs and prmon outputs; uploads with `register_to_rucio.py --noregister` when `COPYLOG` | the proxy |
| Outputs | validates each output (`validate_rootfile.py`, exit 65 on failure); uploads and registers FULL and RECO with dataset tag metadata through `register_to_rucio.py` (exit 78 on failure) | the proxy, JLab Rucio |
| Condor branches | bearer-token discovery under `_CONDOR_CREDS`, `xrdcp` fallbacks when Rucio is off, `.job.ad` and `.machine.ad` dumps | the condor path |

Consequences of this split:

- No produced FULL or RECO file carries an `events` count, so no
  dataset total can be derived (RUCIO_REGISTRATION_CONTRACT.md).
- Registration failure fails the job at its last step, after the
  payload work is done: the 2026-08-31 loss of 4,400 finished
  Perlmutter jobs (RUCIO_RESILIENCE.md). The measures planned there
  need the registration step implemented in this tree.
- The payload report is the pilot's lift of `jobReport.json`
  (`write_job_report` in the dispatcher), which today carries the exit
  code and message only. Events processed, stage timings and CPU, the
  basis for scouts and honest efficiency, are not reported.
- The payload's behavior is bound to the image build: a payload change
  needs a container rebuild, and a task's payload version is not
  recorded anywhere epicprod reads.

## The payload package

The payload lives in this repository under `payload/`: `run.sh`,
`register_to_rucio.py`, `validate_rootfile.py`,
`parse_podio_metadata.py`, `shared_utils.py`, `rucio.cfg`, with a
`VERSION` file. The submit doer ships the directory in the sandbox
beside the dispatcher, from the release the deploy freezes, the way
the canary probe ships its kit (`site-canary`
`probe_kit/build-sandbox.sh`): a submission carries a committed payload
version rather than a working tree. The dispatcher's entry point becomes
`payload/run.sh` in the sandbox; the container path is no longer
referenced. The payload version is written into `jobReport.json` and
recorded on the task's `PandaTasks` row, so every job and task states
what payload ran it.

### Stage 0: the clone

The first commit is a byte-identical copy of the six files from
`eic/simulation_campaign_hepmc3` at a named commit, with one change,
the dispatcher's entry point. Acceptance: one manifest row run through
the container's payload and through the sandbox payload on the same
queue produces FULL and RECO files that agree in event count, podio
metadata and validation, and identical registration records apart
from the DID try namespace. This is the canary payload run of the
submission ladder (CONTINUOUS_PRODUCTION.md § The submission ladder,
rung 2) applied to the payload itself.

## Evolution

In order, each a committed step on the clone:

1. **Event counts at registration.** After a successful upload, the
   `events` tree entry count of each ROOT output is written to its
   file DID, so Rucio derives the dataset total. A count that cannot
   be read or written is reported by file name and the upload stands.
   This is the contract's open row, and the first change the clone
   carries.
2. **Registration resilience** (RUCIO_RESILIENCE.md). Measure 1: a
   randomized delay before registration and one attempt with backoff
   and jitter. Measure 2: the job uploads, makes one attempt, records
   a pending registration in `jobReport.json` on failure and exits
   success on good physics plus completed upload; an ops-agent
   registrar completes pending registrations in batches at bounded
   concurrency; the BNL interim stash when the upload path itself
   fails (RUCIO_FAILOVER_STASH.md). A registration failure then costs
   no completed compute.
3. **Payload reporting.** `jobReport.json` becomes the payload's
   report: events requested and produced per stage, wall and CPU per
   stage from the prmon summaries, peak memory, output sizes, the
   registration outcome and any pending registration, the payload
   version. The pilot lifts it into the job record (pilot 3.14.1.31
   and later), where scouts, sizing and efficiency read it.
4. **PanDA-only shape.** The condor branches go: the bearer-token
   discovery, the `xrdcp` fallbacks, the ad dumps. The environment is
   the one file the doer writes, read by name; exits carry reasons in
   the report; the software stack is the only thing taken from the
   container.
5. **Inputs.** Streaming from the JLab door stays. Rucio-resident
   input by DID (JEDI_INTEGRATION.md § Payload-staged external EVGEN)
   follows once the EVGEN registration coverage is complete.
6. **Internal EVGEN stage** as a payload stage ahead of simulation
   (PCS_DATASET_REQUEST_WORKFLOW.md § Workflow modes), when a
   generator run becomes an epicprod task.

## Container contract

The image supplies the software stack; the payload takes nothing else
from it. What the payload needs from it: the detector setup and
descriptions for the task's `DETECTOR_VERSION`, npsim, eicrecon, ROOT
with PyROOT, the
background merger, `prmon`, `jq`, the xrootd client, and a Python with
the Rucio client and jsonschema. The image is pinned per campaign by
`ProdConfig.container_image`. The presence of the Rucio client and of
uproot in the campaign image is to be confirmed; a missing Python
dependency is vendored in the sandbox, as the canary kit vendors its
package.

## Validation and cut-over

The epicprod path is not yet the production submission path, so the
cut-over carries no compatibility constraint with running production.
Validation is the canary payload run: a one- or two-job task per
configuration through the sandbox payload, outputs compared with a
reference run, before that payload serves a campaign. The 26.09 tasks
submitted through epicprod use the epicprod payload from the first
task. The production team's planned run-script changes for September
land in this tree; the condor submission path keeps its own copy.

## Sequencing

1. Stage 0: the clone under `payload/`, shipped in the sandbox from
   the release, entry point switched, version recorded on job and
   task; canary payload run compared with the container payload.
2. Event counts at registration; verified against a registered
   dataset's derived total.
3. Payload reporting in `jobReport.json`, read on the task and job
   pages and by the scout gate.
4. Registration resilience, Measure 1 then Measure 2, with the
   registrar under the ops agent and the pending-registration view.
5. PanDA-only shape.
6. Rucio-resident inputs; the internal EVGEN stage.

## Asks and open items

- The production team: agreement that PanDA production's payload
  evolves in this tree, and that the September run-script changes land
  here.
- Container: confirmation of the Rucio client, jsonschema and uproot
  in the campaign image, or vendoring in the sandbox.
- Storage operations: the BNL interim stash allocation
  (RUCIO_FAILOVER_STASH.md).
- Credentials unchanged: the `eicprod` proxy shipped in the sandbox
  registers the outputs, as today.
