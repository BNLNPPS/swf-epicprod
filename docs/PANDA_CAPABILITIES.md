# PanDA capabilities and monitoring for GPU worker jobs

An inventory of PanDA mechanisms relevant to the GPU worker track
(`docs/VOLUNTEER_GPU_PLAN.md`), and the monitoring work needed to make
that job class legible on the job and task pages. The GPU jobs on
`BNL_NPPS_GPU` are a new category: a worker outside the facility
perimeter, no grid storage, outputs in an object store, and a payload
whose result is a physics measurement rather than a file count.

Sources surveyed: the PanDA documentation at
<https://panda-wms.readthedocs.io> (in particular
`advanced/task_params.html`), the `splitRule` token table in
`pandaserver/taskbuffer/task_split_rules.py`, the client API in
`pandaclient/Client.py`, and the `prun` option set in
`pandaclient/PrunScript.py`. The documentation covers the task
parameter vocabulary in full; several mechanisms below were reached by
code reading first and confirmed against the documentation afterwards.

## In use today

Task submission (`tools/npps0/submit_test_task.py`): `multiStepExec`
with `containerOptions`, `container_name`, `noInput`, `noOutput`,
`skipScout`, `messageDriven`, `pushStatusChanges`, `cloudAsVO`,
`maxAttempt`, and omission of the `log` entry for object-store mode
(the `prun --noSeparateLog` pattern — with no log dataset, the task
refiner has nothing to validate against Rucio and the Adder has
nothing to register).

Direct job submission (`tools/npps0/submit_direct_job.py`):
`Client.submitJobs` with `destinationSE='local'`, which the Adder
honours as its registration skip, plus `prodSourceLabel='test'` and a
`gangarobot`-prefixed `processingType` to reach the Setupper branch
that keeps the original dataset name.

## Available and unexploited

Grouped by what each would buy. Names are exact.

### Credentials on the worker

- `useSecrets` (task) with `Client.set_user_secret` /
  `Client.get_user_secrets` — secrets stored server-side and delivered
  to the job through PanDA. This is the mechanism that removes
  object-store keys from worker hosts, which is the stated function of
  gateway v0 in the plan. It exists and is unused.

### Object-store and registration handling

- `putLogToOS` — jobs upload log files to the object store. A
  task-level expression of the S3 log path currently arranged through
  queue configuration.
- `registerDatasets` — controls whether the task registers output
  datasets in DDM at all.
- `registerEsFiles`, `mergeEsOnOS` — event-service output registration
  and merging on object storage.
- `altStageOut`, `stayOutputOnSite`, `onSiteMerging` — alternative
  stage-out behaviour and output locality.

### Payload and parameter handling

- `encJobParams` — job parameters base64-encoded rather than passed as
  a quoted string.
- `noExecStrCnv` — the pilot skips execution-string conversion.

Together these address the parameter-quoting handling that motivates
part of the `epicrun` executor (`tools/worker/epicrun.py`); worth
evaluating before that executor's interface is settled.

### Fine-grained and streaming dispatch

The streaming model in the plan (bounded work quanta, re-queue on
worker loss) maps onto existing machinery rather than new
construction:

- `fineGrainedProc` — jobs track processing through the event service
  mechanism.
- `segmentedWork`, `dynamicNumEvents`, `nEventsPerWorker`,
  `maxEventsPerJob`, `tgtNumEventsPerJob` — work segmentation and
  quantum sizing.
- `nEsConsumers`, `nSitesPerJob`, `useJobCloning`,
  `resurrectConsumers`, `switchEStoNormal` — consumer topology and
  recovery.
- `maxAttemptES`, `maxAttemptEsJob`, `decAttOnFailedES`,
  `notDiscardEvents` — retry policy at event-range granularity, which
  is the loss policy for a worker that disappears.
- `runUntilClosed` — a task that continues until its input is closed:
  the steady-state consumer shape.
- `Client.get_events_status` / `Client.update_events` — the quantum
  ledger, readable and writable from the client.

### Heterogeneous fleet brokerage

Relevant to driver and CUDA inhomogeneity across contributed hardware,
the sharpest open technical question for the volunteer track:

- `--architecture` (task) — base OS platform, CPU and GPU
  requirements, applied at brokerage.
- `osMatching`, `ipConnectivity`, `ipStack`, `avoidVP`, `limitedSites`
  — matching by operating system and network class.

### Operations

- `Client.setDebugMode` — turns debug mode on for a **running** job,
  streaming its stdout to the monitor. Suited to diagnosing a remote
  worker without waiting for a log round trip.
- `disableAutoRetry`, `noLoopingCheck`, `useExhausted`,
  `allowPartialFinish`, `allowEmptyInput`, `disableAutoFinish`.
- `Client.getTaskParamsMap`, `prun --dumpTaskParams` — retrieve the
  full parameter map of any task, which supplies working exemplars
  without database queries.

### Data services

- `Client.requestEventPicking` — builds a dataset from specified
  runs and events. A candidate backend for loading a small number of
  events from a running task into an event display.
- `Client.get_files_in_datasets` — server-side file listing for a
  task, a candidate backend for file-list download on the task and
  data-finder pages.
- `Client.getUserJobMetadata` — metadata reported by the payload,
  retrievable per task (see "Reporting from the payload" below).

### Workflows

- `--parentTaskID`, `fullChain`, `intermediateTask` — task chaining.
- `Client.call_idds_command`, `Client.submit_workflow` — iDDS and
  PanDA native workflow submission.

## Documentation map

The PanDA documentation carries subsystem chapters beyond the task
parameter reference, several of which bear on planned work: Working
with iDDS; Working with PanDA Native Workflows; the Messaging
Mechanism (behind `pushJob` and `messageDriven`); the Job Retry
Module (`retryModuleRules`); Brokerage; Job Sizing; Dynamic
Optimization of Task Parameters; Data Carousel.

PanDA also ships an MCP server ("Enabling PandaMCP"; `pandamcp/`
is present in the deployed server tree). Its tool set should be
reviewed before building overlapping tooling.

## Monitoring worklist

The job and task pages are shaped for grid production jobs. For the
GPU worker class they show broken links, uninformative accuracy, and
empty fields, and they omit what the job actually did. Items in
priority order; job 2239921 and task 38938 are the reference cases.

### Defects

1. **Log URL card shows unresolvable entries.** The card is built by
   splitting `pilotid` on `|` and treating the first field as a URL
   (`monitor_app/panda/queries.py`, `study_job`). A standalone pilot
   with no batch system publishes no URL, so the field is the literal
   string `unknown` and the card renders three dead links. The card
   should be suppressed when the field is not a URL, and for
   object-store queues replaced by the S3 object locations, whose keys
   are composed deterministically as
   `logs/<queue>/<log dataset>/<lfn>`.
2. **The payload log is unreachable.** The Payload Log card depends on
   a log file fetched from a Rucio tarball. For perimeter-external
   workers the tarball is in the object store. An S3-aware fetch makes
   the card work; the monitor host already holds the credentials and
   the client library. This is the highest-value single change, since
   that tarball contains everything the remaining items need.
3. **Log analysis runs only for failed jobs**, and only against a log
   browser that does not hold these logs. A finished GPU job has no
   path from the page to its own result.

### Accuracy and noise

4. **The transformation field names `runGen`,** which is correct and
   uninformative: the executed command is URL-encoded inside the job
   parameter blob. The page needs a decoded payload command displayed
   near the top. When `epicrun` replaces `runGen`, this becomes a
   structured field rather than a decode.
5. **Grid-shaped fields render empty** for this class — compute
   element, worker node, request ID, production phase and failure
   fields. Empty fields should be suppressed, or the class given its
   own field set.

### The narrative

6. **A summary panel stating what the job did**, readable at a glance:
   the payload command; the container and host; the GPU model, count
   and driver version; and the payload result — for the optical
   photon test, photons propagated, hits stored, GPU time per event,
   and the pass or fail verdict. Every one of these facts is present
   in the payload log and none is in the PanDA schema.
7. **A GPU section in the resource card**: model, count, device
   identifiers, driver, CUDA and OptiX versions, and GPU time against
   wall time. Note that the conventional resource fields are nearly
   empty for these jobs (`maxpss` unset, `jobmetrics` carrying only
   `workDirSize`), so the card is currently close to blank.
8. **An object-store output panel**: each output with size, object
   key, and a working retrieval link, in place of the grid-shaped file
   table. This is also where a file-list download belongs.

### Task page

9. Mirror the log and payload treatment at task level, and state
   object-store mode explicitly for tasks with no log dataset rather
   than rendering empty sections that read as breakage.
10. Task-level aggregates over the GPU dimension: total GPU seconds,
    events, hits, and mean time per event across jobs.
11. A worker dimension: which machine ran each job, with a per-worker
    rollup. With one host this is a label; with a contributed fleet it
    is the contribution view, and the field costs nothing to design in
    now.

## Reporting from the payload

Items 6, 7 and 10 above are satisfied today only by parsing payload
logs. The durable alternative is for the payload to report structured
values: `epicrun` (`tools/worker/epicrun.py`) is under this project's
control and can emit a small JSON record — GPU model and driver,
photons, hits, time per event, verdict — delivered through job metrics
or user job metadata and retrievable with
`Client.getUserJobMetadata`. The pages then read fields instead of
scraping text, and every subsequent GPU payload is described the same
way without further monitoring work.
