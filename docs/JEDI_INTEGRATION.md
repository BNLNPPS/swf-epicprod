# JEDI Integration — Direct Task Submission from PCS

## Overview

epicprod is the ePIC automated production system. It combines the PCS
configuration/catalog subsystem, PanDA monitoring views, the credentialed
production operations agent, Rucio lineage flows, and production-status
interfaces. PCS composes physics, event generation, simulation, reconstruction,
and optional background tags into production tasks; the live epicprod submit path
sends external-EVGEN tasks directly to JEDI via the PanDA Python API through the
prod-ops agent.

This document describes the integration design: how PCS task parameters map to
JEDI's `taskParamMap`, the submission flow, and the current operational paths.

**Approach:** Direct API submission. PCS owns the full task specification. JEDI's existing `GenTaskRefiner` handles the task — no custom server-side plugin required.

## Reference implementation — the working basis

Our EVGEN submission is derived from the EIC production submitter maintained by Sakib Rahman in `eic/job_submission_condor`, branch **`feature-add-panda-wrapper`**. This is the authoritative working template; `scripts/evgen_panda_submit.py` in this repo is adapted from it, and any divergence in submission behavior is checked against it first.

- [`scripts/submit_csv.sh`](https://github.com/eic/job_submission_condor/blob/feature-add-panda-wrapper/scripts/submit_csv.sh#L107-L151) — PanDA-mode entry: stages the sandbox, derives the `group.EIC.<dataset>` name from the detector version/config and the CSV's first path, and builds the `submit_panda_api.py` command line.
- [`scripts/submit_panda_api.py`](https://github.com/eic/job_submission_condor/blob/feature-add-panda-wrapper/scripts/submit_panda_api.py) — builds the `taskParamMap` (`noInput=True`, `noOutput=True`; container `multiStepExec`; `%RNDM`→`${SEQNUMBER}` pseudo-input; sandbox tarball via `Client.putFile`) and submits with `panda_api.get_api().submit_task(params)`.
- [`scripts/submit_panda.py`](https://github.com/eic/job_submission_condor/blob/feature-add-panda-wrapper/scripts/submit_panda.py) — the per-job in-container payload: reads row *n* of the chunked CSV and runs the hepmc3 campaign `run.sh`.

Submission chain: `submit_csv.sh` → `submit_panda_api.py` (`client.submit_task`); `submit_panda.py` is the payload invoked per `${SEQNUMBER}`.

## Architecture

```
┌─────────────────────────────────────────────────┐
│  epicprod / PCS configuration layer             │
│                                                 │
│  PhysicsTag ─┐                                  │
│  EvgenTag   ─┼─► Dataset ─┐                     │
│  SimuTag    ─┘             ├─► ProdTask          │
│  RecoTag   ─┘   ProdConfig┘     │               │
│                                  │               │
│                   build_task_params(task)        │
│                          │                      │
│                          ▼                      │
│                   taskParamMap (dict)            │
│                          │                      │
│                 submit_to_jedi(task)             │
│                          │                      │
└──────────────────────────┼──────────────────────┘
                           │ Client.insertTaskParams()
                           ▼
┌─────────────────────────────────────────────────┐
│  PanDA Server                                   │
│                                                 │
│  POST /api/v1/task/submit                       │
│       │                                         │
│       ▼                                         │
│  TaskBuffer.insertTaskParamsPanda()             │
│       │  stores task in DB, state = "defined"   │
│       ▼                                         │
│  JEDI TaskRefiner daemon                        │
│       │  selects GenTaskRefiner via VO config   │
│       ▼                                         │
│  GenTaskRefiner.extractCommon()                 │
│  GenTaskRefiner.doRefine()                      │
│       │  creates JediTaskSpec + dataset specs   │
│       ▼                                         │
│  ContentsFeeder → JobGenerator → JobBroker      │
│       │  breaks task into jobs, assigns sites   │
│       ▼                                         │
│  Jobs dispatched to Pilot                       │
└─────────────────────────────────────────────────┘
```

## PCS-to-JEDI Field Mapping

### Task Identity

| JEDI Parameter | PCS Source | Notes |
|---------------|-----------|-------|
| `taskName` | physical PanDA attempt name | first attempt uses the PCS composed identity; retries/site races append `.tryN`; see [Output dataset and file naming](#output-dataset-and-file-naming) |
| `userName` | `task.created_by` | PCS user who created the task |
| `vo` | `'eic'` | Virtual organization |
| `workingGroup` | `config.panda_working_group` | e.g. `'EIC'` |
| `campaign` | Derived from detector version | e.g. `'26.02.0'` |

### Processing Definition

| JEDI Parameter | PCS Source | Notes |
|---------------|-----------|-------|
| `prodSourceLabel` | `config.data['prod_source_label']` | `'test'` during commissioning; production submissions may use `'managed'` |
| `taskType` | `'prod'` | PanDA task type for the live client-API EVGEN path |
| `processingType` | `config.data['processing_type']` | e.g. `'epicproduction'` |
| `taskPriority` | `config.data` or default | 0-1000, production typically 900 |
| `transPath` | `config.data['transformation']` | Payload executable or TRF URL |
| `transUses` | `''` | Not used for containerized jobs |
| `transHome` | `''` | Not used for containerized jobs |
| `architecture` | `''` | Empty string — container handles platform |
| `container_name` | `config.container_image` | Singularity/Docker image reference |

### Job Splitting

| JEDI Parameter | PCS Source | Notes |
|---------------|-----------|-------|
| `nEventsPerJob` | `config.data['events_per_job']` | Events per individual job |
| `nEvents` | `config.events_per_task` | Total events for the task |
| `nFiles` | `config.data['n_jobs']` | When using noInput, this controls job count |
| `nFilesPerJob` | `config.data['files_per_job']` | Input files per job (default 1) |
| `noInput` | `True` | MC generation has no input dataset |
| `coreCount` | `config.data['corecount']` | CPU cores per job (default 1) |
| `walltime` | Derived from `config.target_hours_per_job` | In seconds for JEDI |
| `ramCount` | `config.data` or GenTaskRefiner default | MB per core (default 2000) |

### Site Selection

| JEDI Parameter | PCS Source | Notes |
|---------------|-----------|-------|
| `site` | `config.panda_site` | PanDA queue name, e.g. `'BNL_EPIC_PROD_1'` |
| `cloud` | `config.panda_working_group` or `'US'` | GenTaskRefiner copies workingGroup to cloud |

### Output Datasets

| JEDI Parameter | PCS Source | Notes |
|---------------|-----------|-------|
| `log` | Built from the output dataset name | Log dataset template (`<scope>:<name>.log`) |
| `jobParameters` | Built from config + tags | Execution command + tag-based output file template |

### Flags

| JEDI Parameter | PCS Source | Notes |
|---------------|-----------|-------|
| `skipScout` | `config.data['skip_scout']` | Skip scout jobs if True; the Prod Config UI presents this as the positive **Scout Mode** toggle |
| `disableAutoRetry` | `config.data` | Optional |
| `useRucio` | `config.use_rucio` | Whether to register outputs in Rucio |

## Output dataset and file naming

The output carries two naming conventions, applied at different levels.

### Output dataset (Rucio DID)

The PCS task has two names:

- the **logical campaign task identity**, the dataset's composed identity name —
  classification tags plus any sample-variant discriminator, defined in
  [PCS.md](PCS.md#datasets);
- the **physical PanDA attempt name**, used as `taskName`/`outDS` for a concrete
  JEDI submission.

The first PanDA attempt uses the logical identity as its physical name:

    {scope}.{detector_version}.{detector_config}.{physics_tag}.{evgen_tag}.{simu_tag}.{reco_tag}[.{background_tag}][.{sample_name}]

Additional attempts append a reserved retry discriminator allocated by PCS before
submission:

    {scope}.{detector_version}.{detector_config}.{physics_tag}.{evgen_tag}.{simu_tag}.{reco_tag}[.{background_tag}][.{sample_name}].try2

The suffix is `tryN`, not part of the logical task identity. It exists so
resubmissions and site races have unique PanDA task names and unique Rucio output
dataset names. `tryN` is a reserved final token and may not be used as a sample
name segment. If a Rucio block suffix is present, the canonical physical form is
`logical.try2.b1`; parsing strips terminal suffixes from the right so both
`logical.b1` and `logical.try2.b1` resolve to the same logical PCS identity.
The suffix rules are defined centrally in `src/pcs/name_tokens.py` and
documented in [PCS.md](PCS.md#composed-name-suffixes).

`{detector_version}` is the version of the detector/software conditions for the
produced data. Campaign membership is production bookkeeping and does not rename
the dataset identity. The PanDA task name is this name without any trailing
`.bN` block suffix; the Rucio DID is the same physical attempt name with the
`{scope}:` prefix and the block suffix (`group.EIC:….r1.45to135deg.b1` or
`group.EIC:….r1.45to135deg.try2.b1`).

The composed name is the **logical** dataset identity. Physical PanDA/Rucio
attempt names are derived from it, with `.tryN` added only when uniqueness
requires another concrete submission namespace. This supersedes the path-based
RECO DID (`/RECO/<campaign>/<detector_config>/<suffix>`) built by
`_output_dataset_name` (commit 6ea0d8e); that builder and the "path is the
identity" convention are retired on the PanDA side. ePIC's slash paths remain in
use only as Rucio references for external EVGEN inputs and for the data-lineage
sweep (see [EPICPROD_DATA_LINEAGE.md](EPICPROD_DATA_LINEAGE.md)) — never to name
PanDA outputs. Samples that share a tag composition — single-particle datasets
differing only by polar-angle range — are told apart by the `{sample_name}`
segment (`3to50deg`, `45to135deg`), so they no longer collide and need no
path-based name.

### Output files (LFN)

Logical file names carry the composed identity name — the dataset name with the
`{scope}.` prefix and `.bN` block suffix removed:

    {detector_version}.{detector_config}.<p>.<e>.<s>.<r>[.<k>][.<sample_name>].$PANDAID.${SN}.edm4eic.root
    {detector_version}.{detector_config}.<p>.<e>.<s>.<r>[.<k>][.<sample_name>].$PANDAID.log.${SN}.log.tgz   (log)

The LFN base is thus literally our composed name, using the same dot separator as
the DID — decoupled from ePIC's `/RECO/...` slash convention, which PCS uses only
for input/lineage references, never to name PanDA outputs. The `{sample_name}`
segment distinguishes the variant datasets of one composition at the file-base
level. `$PANDAID` and `${SN}` are PanDA template variables substituted
server-side when jobs are generated: `${SN}` is a per-task serial number
(`task_complex_module.py`), and `$PANDAID` is the globally unique PanDA job id,
substituted unconditionally (`job_complex_module.py`, line 3056) — it keeps each
file name unique even across variant datasets, and needs no identifier known at
build time. File names are resolved through Rucio by dataset, so the LFN form
does not affect dataset-level discoverability.

`$JEDITASKID`, the task-level identifier, is also available but is substituted
only for non-`managed` jobs (`job_complex_module.py`, line 3058); `$PANDAID`
applies to managed production as well.

## Example: taskParamMap Built from PCS

Given a ProdTask with:
- Dataset: `group.EIC.26.02.0.epic_craterlake.p3001.e1.s1.r1` (DIS NC 10x100)
- ProdConfig: container image, 100 events/job, 1000 total events, 1 core

The `build_task_params(task)` function would produce:

```python
taskParamMap = {
    # Identity
    'taskName': 'group.EIC.26.02.0.epic_craterlake.p3001.e1.s1.r1',
    'userName': 'wenaus',
    'vo': 'eic',
    'workingGroup': 'EIC',
    'campaign': '26.02.0',

    # Processing
    'prodSourceLabel': 'test',
    'taskType': 'prod',
    'processingType': 'epicproduction',
    'taskPriority': 900,

    # Executable (containerized)
    'transPath': 'https://pandaserver-doma.cern.ch/trf/user/runGen-00-00-02',
    'transUses': '',
    'transHome': '',
    'architecture': '',
    'container_name': 'docker://eicweb/jug_xl:26.02.0-stable',

    # Splitting
    'noInput': True,
    'nFiles': 10,           # number of jobs
    'nFilesPerJob': 1,
    'nEventsPerJob': 100,
    'coreCount': 1,
    'ramCount': 4000,
    'ramUnit': 'MBPerCore',

    # Site
    'site': 'BNL_EPIC_PROD_1',
    'cloud': 'EIC',

    # Log output
    'log': {
        'dataset': 'group.EIC:group.EIC.26.02.0.epic_craterlake.p3001.e1.s1.r1.log',
        'type': 'template',
        'param_type': 'log',
        'token': 'local',
        'destination': 'local',
        'value': '26.02.0.epic_craterlake.p3001.e1.s1.r1.$PANDAID.log.${SN}.log.tgz',
    },

    # Job parameters: execution command + output file spec
    'jobParameters': [
        {
            'type': 'constant',
            'value': (
                'EBEAM=10 PBEAM=100 '
                'DETECTOR_VERSION=26.02.0 DETECTOR_CONFIG=epic_craterlake '
                'JUG_XL_TAG=26.02.0-stable '
                'COPYRECO=true COPYFULL=false COPYLOG=true '
                './run.sh'
            ),
        },
        {
            'type': 'template',
            'param_type': 'output',
            'token': 'local',
            'destination': 'local',
            'dataset': 'group.EIC:group.EIC.26.02.0.epic_craterlake.p3001.e1.s1.r1',
            'value': '26.02.0.epic_craterlake.p3001.e1.s1.r1.$PANDAID.${SN}.edm4eic.root',
            'offset': 1000,
        },
    ],
}
```

In both modes `taskName` and the output dataset are the composed identity name;
the modes differ only in how the sample is produced, not in how it is named. The
example above is the generation-only case (`noInput=True`, no input path). A
single-particle external-EVGEN task (a `csv_manifest` input) would instead be,
for example, `group.EIC.26.02.0.epic_craterlake.p1141.e37.s1.r1.130to177deg`,
with LFN base `26.02.0.epic_craterlake.p1141.e37.s1.r1.130to177deg` and the input
staged as described below.

## External EVGEN Inputs

The example above is the pure MC-generation case (`noInput=True`). PCS also
needs to submit production tasks that consume **externally supplied EVGEN
files** — generator-level samples produced by PWGs/DSCs and described to PCS
as a CSV manifest (or other external source). See
[PCS_DATASET_REQUEST_WORKFLOW.md](PCS_DATASET_REQUEST_WORKFLOW.md) for the
PCS-side dataset model.

### Mode summary

| Mode | When | `noInput` | How the payload sees the input |
|------|------|-----------|----------------------------|
| Generation-only | No external EVGEN; payload generates events | `True` | n/a |
| External EVGEN — payload-staged (filesystem) | A CSV manifest names input files on a filesystem path | `True` | `CSV_FILE=<location>` env var; the payload downloads and stages the listed files at runtime |
| External EVGEN — payload-staged (Rucio-resident) | Input is a dataset registered in JLab Rucio | `True` | input DID passed via env; the payload pulls the registered dataset from JLab Rucio at runtime |
| External EVGEN — JEDI-driven | Input is a Rucio dataset resolved by JEDI | `False` | `--inDS`/`pfnList`; does not apply — see the single-Rucio constraint below |

### Data handling and the single-Rucio constraint

A PanDA server is configured against one Rucio instance. The BNL PanDA server
used for ePIC production uses BNL Rucio, where PanDA registers job logs. ePIC
production data — EVGEN inputs and RECO/FULL outputs — is held in JLab Rucio, a
separate instance. The production payload performs all science-data movement
directly against JLab Rucio, so PanDA does not resolve, transfer, or register
the science data on either the input or the output side.

JEDI-driven input (`--inDS`/`pfnList`, `noInput=False`) would require JEDI to
resolve the input DID and its replicas through PanDA's Rucio, which is BNL Rucio,
where the EVGEN is absent; it therefore does not apply. Rucio-resident EVGEN
input is staged by the payload from JLab Rucio instead: the registered input DID
is passed to the payload through the environment, by the same mechanism as the
filesystem `CSV_FILE` path, and `noInput` remains `True`. This keeps the input
side consistent with the output side, with PanDA out of the science-data path.

### Payload-staged external EVGEN

This matches the existing condor-side wrapper convention: PCS already passes
`CSV_FILE=<csv_path>` as an environment variable to the condor job, and the
payload's `run.sh` reads it. We carry the same convention into JEDI.

The PCS source for the value is the linked `Dataset.metadata.source.location`
on the input dataset (kind `csv_manifest`), exposed via
`ProdTask.input_source_location`.

`build_task_params(task)` injects the value into the **payload command
string** in `jobParameters[0]['value']` when the task has an external input
source. The job dictionary keeps `noInput=True` and the rest of the
generation-mode shape — the only change is one extra `CSV_FILE=...` env in the
constant string. Example:

```python
'jobParameters': [
    {
        'type': 'constant',
        'value': (
            'EBEAM=10 PBEAM=100 '
            'DETECTOR_VERSION=26.02.0 DETECTOR_CONFIG=epic_craterlake '
            'JUG_XL_TAG=26.02.0-stable '
            'CSV_FILE=campaigns/26.02.0/dis_nc_10x100.csv '   # ← only this line is added
            'COPYRECO=true COPYFULL=false COPYLOG=true '
            './run.sh'
        ),
    },
    # output template unchanged
]
```

This is **not** a JEDI parameter. It is an env var the payload reads. JEDI
sees an opaque constant string and stages it into each job's command line as
written.

### Sample-variant submit_params

A sample variant's `submit_params` (e.g. the gun angle bounds for an angular
range; see [PCS.md](PCS.md#sample-variants)) reach the job mode-dependently. In
generation mode they are injected into the payload command string
(`jobParameters[0]`) as gun parameters — the same mechanism as `CSV_FILE` above.
In external-EVGEN mode the discriminator is already carried by the input
path/manifest, so `submit_params` is empty. The exact parameter spelling is
fixed in implementation planning.

### What this does *not* require

- No new JEDI fields. `pfnList`, input-dataset references, and `nFilesPerJob`
  semantics for inputs all stay untouched.
- No GenTaskRefiner behavior change.
- No PanDA-team confirmation beyond what the all-`noInput` plan already
  needed.

The PCS-side mechanism is the only change: `ProdTask.input_dataset` (FK to a
`Dataset(stage=evgen, source.kind=csv_manifest)`) replaces the legacy
`ProdTask.csv_file` string as the source of truth, and `build_task_params`
reads from it.

## GenTaskRefiner Behavior

When JEDI processes this task, `GenTaskRefiner` (61 lines, `panda-server/pandajedi/jedirefine/GenTaskRefiner.py`) applies these defaults:

1. **`cloud`** — if absent, copies from `workingGroup` (so `cloud='EIC'` from `workingGroup='EIC'`)
2. **`transPath`** — defaults to `runGen-00-00-02` TRF if not set (we set it explicitly)
3. **`ramCount`** — defaults to 2000 MB if not set
4. **`pushStatusChanges`** — defaults to True (status updates via message queue)
5. **`messageDriven`** — defaults to True
6. **`cloudAsVO`** — always set to True (cloud field used as VO for brokerage)
7. **Dataset templates** — instantiated per-site if DDM interface is available

The `GenJobBroker` then handles site selection using the simplified non-ATLAS brokerage logic: filter by queue status, disk space, walltime constraints, then select.

## Client-API EVGEN submission

The live Submit path is the client-API EVGEN submission, which reproduces the
proven condor-side recipe (`eic/job_submission_condor`, `submit_panda_api.py`) as
code owned by this repo and runs it inside the prod-ops agent. It supersedes the
prun path for the Submit button; prun (`build_panda_command`,
`scripts/submit-prod-task.py`) is retained but unwired.

A production EVGEN task is `noInput=True`+`noOutput=True`: the containerized
payload streams its EVGEN input from JLab over xrootd and self-registers RECO to
JLab Rucio, so PanDA handles no science data (the single-Rucio constraint above).
The `taskParamMap` wraps the generic `runGen` TRF through `multiStepExec`, carries
a `sourceURL`, and turns `%RNDM=0` into a per-job `${SEQNUMBER}`, so one job runs
per manifest row.

### Components

- **`commands.build_evgen_task_params(task, panda_tasks=None)`** (`?fmt=evgen`) — the
  credential-free spec. It resolves the task's matched JLab Rucio EVGEN DID(s)
  (`Dataset.metadata['rucio']['matched']`, written by the EVGEN assimilation) to
  their files over the public `eicread` read, and emits one manifest row
  (`file,ext,nevents,ichunk`) per file. A Rucio file's name is the xrootd path
  below `EVGEN/` — the payload prepends `root://…/volatile/eic/EPIC/` to
  `EVGEN/<file>`. The EVGEN files are registered without their per-file event counts
  (Rucio's native `events` field is unpopulated), so `nevents` is the
  configured per-job count (`events_per_job`) and there is one job per file.
  `outDS` is the physical PanDA attempt name: the PCS composed identity for the
  first try, or that identity plus `.tryN` for later tries; under `noOutput` it is
  the PanDA task name only.
  Payload environment is derived from PCS as well: output flags and `OUT_RSE`
  come from `ProdConfig`, `LOG_RSE` comes from `ProdConfig.data['log_rse']`, and
  background-mixing fields (`SIGNAL_FREQ`, `SIGNAL_STATUS`, `TAG_PREFIX`,
  `BG_FILES`) prefer the dataset's background tag (`k`) parameters, falling back
  to legacy EvGen tag parameters when no `k` value is present.
- **`scripts/evgen_panda_submit.py`** — the submission kernel, this repo's owned
  port of `submit_panda_api.py`: it builds the `taskParamMap`, uploads the
  sandbox to the PanDA cache, and submits via `pandaclient` under the operator's
  OIDC token.
- **`scripts/submit-evgen-task.py`** — the credentialed doer, the EVGEN
  counterpart of `submit-prod-task.py`. It fetches the spec, assembles the
  sandbox (the manifest, the `environment-*.sh` the payload sources, the in-job
  dispatcher, staged `BG_FILES` JSON when configured, and the JLab x509 proxy),
  runs the kernel under the panda-client environment, and records the jediTaskID
  back via `record-submission`.
- **`scripts/evgen_job_dispatcher.py`** — shipped in the sandbox. In-job it reads
  the manifest row for its `${SEQNUMBER}` and invokes the payload
  (`/opt/campaigns/hepmc3/scripts/run.sh`), which sources `environment*.sh` from
  the unpacked sandbox.
- **`agents/epicprod_ops_agent.py`** — the `submit_evgen_task` handler and doer,
  deduped per task, emitting the same `prodtask_submitted` /
  `prodtask_submit_failed` / `prodtask_submit_unrecorded` SSE events as the prun
  path. The same agent handles `panda_task_operation` requests for native PanDA
  retry operations on an existing JEDI task.
- **`scripts/panda-task-operation.py`** — the credentialed doer for existing
  PanDA tasks. It sources the local panda-client environment, sets
  `PANDA_AUTH_VO=EIC.production`, calls `increase_attempt_nr` or `retry_task`,
  and returns nonzero when the PanDA client reports a nonzero operation result.

### Output authentication

The payload registers RECO to JLab Rucio as `eicprod`. The doer ships the
`eicprod` x509 proxy in the sandbox — the proven condor-template method
(`submit_csv.sh` copies the proxy in; the payload's `run.sh` reads it back through
`environment*.sh` → `rucio.cfg`). This is settled and needs no verification: the
working condor jobs register their output with this same `eicprod` credential, so
its success there is the proof it authenticates as `eicprod@JLab`.

The proxy is named by `EVGEN_X509_PROXY` and shipped verbatim — **there is no
fallback** (no silent default). It is **not** `X509_USER_PROXY`
(`longproxy-for-rucio`), the agent's BNL Rucio metadata and log-fetch credential
(account `panda`), which does not write JLab output; shipping it would break the
pattern. The operator points `EVGEN_X509_PROXY` at the `eicprod` proxy; the web
tier and the MCP server hold no credential.

### Commissioning defaults

Scouts are off on this path when `skip_scout=true`, so the walltime is used
directly and the `noInput` pseudo-input HS06 brokerage pitfall is avoided. The
Prod Config UI exposes this as **Scout Mode**: checked means scouts run, unchecked
means `skip_scout=true`. New configs use the last saved toggle state from the
user's database-backed JSON preferences; with no remembered value, scout mode
starts off.

## Implementation status (2026-06-29)

The **live** Submit path is the **client-API EVGEN doer** (`submit_evgen_task`),
described under [Client-API EVGEN submission](#client-api-evgen-submission). The
prun path of 2026-06-03 — `build_panda_command` + `scripts/submit-prod-task.py`,
which produced the first managed submission (jediTaskID 36439, see
[EPICPROD_OPS.md](EPICPROD_OPS.md)) — is retained but unwired from the button.

`build_task_params` (the generation-only `taskParamMap`, `?fmt=jedi`) remains a
preview: `noInput` with JEDI-managed outputs, without the `noOutput`, sandbox, and
`multiStepExec` the production payload needs. The client-API path (`?fmt=evgen`)
is the production form.

PCS records every physical PanDA submission in `PandaTasks`. Attempt 1 uses the
logical campaign task identity; later full reruns use `.tryN` physical names.
PanDA tasks submitted outside PCS can be linked dynamically when their PanDA task
name exactly matches one PCS campaign task identity or physical attempt name.

The campaign task compose page is the PCS operator surface inside epicprod. It
shows the `PanDA Tasks` table and these actions:

- **Submit to PanDA** for a task with no current PanDA task.
- **Add Another Retry** for `increase_attempt_nr(jediTaskID, 1)` on an existing
  task; the focused campaign-task page shows `current nmax: N` when PanDA job
  rows expose the current maximum attempt count.
- **Restart And Retry Failures** for `retry_task(jediTaskID,
  new_parameters={})` on an existing task.
- **Rerun Entire Task** for a new `.tryN` physical submission.

The monitor task and job pages provide PanDA state, job counts, final-failure
rates, payload-log access, and links back to the associated campaign task.

## Operator paths

Normal submission is from the campaign task compose page. The web tier queues the
request to the prod-ops agent, and the agent performs the credentialed PanDA
operation. Browser updates arrive through the SSE relay, with a bounded polling
backstop for submission recording.

`pcs-task-cmd` (documented in [PCS.md](PCS.md)) remains useful for inspecting
generated artifacts and for manual debugging. It is not the normal production
submit path.

## Infrastructure: What We Know

- **VO**: `eic`
- **Queues**: 13 EIC queues online (BNL_EPIC_PROD_1, BNL_OSG_EPIC_PROD_1, NERSC_Perlmutter_epic, E1_BNL, E1_JLAB, etc.). All support Apptainer containers.
- **Auth**: OIDC with `PANDA_AUTH=oidc`, `PANDA_AUTH_VO=eic`
- **Output**: Rucio integration available; `token='local'` / `destination='local'` for local staging

## PanDA follow-up items

1. **GenTaskRefiner registration** for `eic:managed` in `panda_jedi.cfg`.
2. **Non-interactive production credential** for unattended operation. Current
   production submissions run through the operator's cached PanDA client token on
   the production host; a service credential remains an operations improvement,
   not a blocker for the deployed path.
3. **`transPath`** default policy for containerized EIC jobs.

## Key References

### PanDA Documentation (panda-docs repo)
- [Task Parameters](../../../panda-docs/docs/source/advanced/task_params.rst) — splitRule codes and parameter priority
- [JEDI Architecture](../../../panda-docs/docs/source/architecture/jedi.rst) — task flow, agents, state machines
- [Client API](../../../panda-docs/docs/source/client/panda-client.rst) — Python API setup and usage
- [Admin Guide](../../../panda-docs/docs/source/admin_guide/admin_guide.rst) — GenTaskRefiner config examples

### PanDA Source Code (cloned in github/)
- `panda-server/pandajedi/jedirefine/GenTaskRefiner.py` — the refiner our tasks will use
- `panda-server/pandajedi/jedirefine/TaskRefinerBase.py` — extractCommon() parameter processing
- `panda-server/pandajedi/jeditest/addNonAtlasTask.py` — non-ATLAS submission example
- `panda-client/pandaclient/example_task.py` — client-side task dict example
- `panda-client/pandaclient/panda_api.py` — `submit_task()` high-level API
- `panda-client/pandaclient/Client.py:1304` — `insertTaskParams()` implementation

### Working submitter — reference basis (eic/job_submission_condor)
Branch **`feature-add-panda-wrapper`** — Sakib Rahman's working EVGEN production submitter (see "Reference implementation" above):
- [`scripts/submit_csv.sh`](https://github.com/eic/job_submission_condor/blob/feature-add-panda-wrapper/scripts/submit_csv.sh#L107-L151)
- [`scripts/submit_panda_api.py`](https://github.com/eic/job_submission_condor/blob/feature-add-panda-wrapper/scripts/submit_panda_api.py)
- [`scripts/submit_panda.py`](https://github.com/eic/job_submission_condor/blob/feature-add-panda-wrapper/scripts/submit_panda.py)

### PCS Source Code (swf-monitor)
- `src/pcs/models.py` — ProdTask, ProdConfig, Dataset, tag models
- `src/pcs/commands.py` — task parameter generation for PCS submission artifacts
- `docs/PCS.md` — PCS documentation
