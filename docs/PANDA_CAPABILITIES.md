# PanDA capabilities and monitoring for GPU worker jobs

An inventory of PanDA mechanisms relevant to the GPU worker track
(`docs/VOLUNTEER_GPU_PLAN.md`), and the monitoring work needed to make
that job class legible on the job and task pages. The GPU jobs on
`BNL_NPPS_GPU` are a new category: a worker outside the facility
perimeter, no grid storage, outputs in an object store, and a payload
whose result is a physics measurement rather than a file count.

Sources surveyed: the PanDA documentation at
<https://panda-wms.readthedocs.io>, in particular
`advanced/task_params.html`, which documents the task parameter
vocabulary in full; the `splitRule` token table in
`pandaserver/taskbuffer/task_split_rules.py` (108 toggles); the client
API in `pandaclient/Client.py` (67 functions); and the `prun` option
set in `pandaclient/PrunScript.py` (109 options). The client scripts
expose a subset of what the API and the task parameter map accept, and
raw task submission reaches mechanisms `prun` does not surface.

A process note worth recording: several problems solved by code
reading during the first GPU integration had documented answers in the
PanDA documentation. Doc-first applies to external projects, not only
to this one.

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

## How the first GPU worker was brought up

The `BNL_NPPS_GPU` queue went from a CRIC definition to finished GPU
jobs with outputs in the object store on 2026-08-13 and 2026-08-14.
The table records what carries each function today and, where the
survey above identifies one, the better route.

| Function | In place | Better route |
|---|---|---|
| Queue existence | CRIC entry cloned from `BNL_PanDA_1` with compute elements dropped; `--nv` in `container_options`, GPU resource type, 86400 s maxtime | — |
| Queue behavior | `tools/npps0/config/queuedata.json` in git, copied into each pass directory, where the BNL pilot wrapper prefers it over the CRIC cache | — |
| Storage catalog | `tools/npps0/config/agis_ddmendpoints.json` seeded under the pilot info system's LOCAL and USER cache filenames with `PILOT_HOME` set to the pass directory; the wrapper's own `--storagedata-url` rewritten in a per-pass copy of the wrapper | a wrapper that respects a pre-set storage-data URL would remove the rewrite |
| Worker provisioning | CVMFS pilot wrapper in pull mode under a launcher loop: `-e eic`, `--pilot-user epic`, `--url` and `-p` for the dispatch endpoint, `--rucio-host`, `--getjobrequests 30`; per-GPU flock, `KillMode=process`, `timeout` reaping | — |
| Container | CVMFS unpacked image directory (a local SIF is not accepted); `container_name` on the job specification — `multiStepExec` container options alone do not trigger containerization | `--alrb`, `--wrapExecInContainer`, `--oldContMode` unexplored |
| Task shape | cloned from a live task's stored parameters; explicit log LFN template, since `${LOG0}` is resolved client-side and never reaches JEDI | `Client.getTaskParamsMap`, `--dumpTaskParams` for exemplars |
| Payload delivery | `runGen` with a URL-encoded command string; payload source cloned from GitHub at run time | `encJobParams` with `noExecStrCnv`; sandbox tarball; `epicrun` |
| Log and output stage-out | `s3` copytool selected through `acopytools` and `astorages` in the git queuedata, against a `DEV_CLOUD_S3` object-store entry using an `https` endpoint (boto3 rejects the `s3://` scheme used by older catalog entries); `PANDA_PILOT_AWS_PROFILE` names the worker profile | `putLogToOS`, `registerDatasets` |
| Object-store credentials | AWS profile file on the worker | `useSecrets` with `Client.set_user_secret` |
| Completion without Rucio, tasks | the `log` entry omitted from the task parameter map | — this is the clean route |
| Completion without Rucio, jobs | `destinationSE='local'`, log dataset pre-created in Rucio, `prodSourceLabel='test'` with a `gangarobot`-prefixed `processingType` | superseded by the task route above |

Four behaviours were established by experiment and are not stated in
the documentation:

- Job dispatch to a pilot started with `-j managed` serves the
  `managed`, `test` and `prod_test` source labels. A `ptest` job is
  accepted by the server, reaches `activated`, and is never offered to
  the pilot.
- The Setupper keeps the original output dataset name for the `panda`
  source label, for a `gangarobot`-prefixed processing type, and for
  ptest-family labels with `pathena` or `prun` processing types.
  Otherwise it mints a `_sub` dataset, whose registration is skipped
  when the destination is `local`, after which the lookup of that
  dataset's identifier fails and the job fails in setup.
- Pilot configuration is read once, at pass start. A configuration
  deployed during a pass reaches jobs only from the following pass;
  jobs dispatched to a pass that began earlier run under the earlier
  configuration.
- Virtual organization strings are case-sensitive at the
  authentication endpoint: `EIC.production`, not lowercase.

## Available and unexploited

Grouped by what each mechanism would buy. Names are exact; task
parameters and `splitRule` toggles are given as bare names, client
functions as `Client.<name>`, and client script options with their
leading dashes.

### Credentials on the worker

- `useSecrets` with `Client.set_user_secret` and
  `Client.get_user_secrets` — secrets stored server-side and delivered
  to the job through PanDA. This is the mechanism that removes
  object-store keys from worker hosts, which is the stated function of
  gateway v0 in the plan. It exists and is unused.
- `Client.get_cert_attributes`, `Client.get_user_name_from_token`,
  `Client.get_new_token` — identity and token handling from the
  client.

### Object-store handling and registration

- `putLogToOS` — jobs upload log files to the object store; a
  task-level expression of the log path currently arranged through
  queue configuration.
- `registerDatasets` — controls whether the task registers output
  datasets in DDM at all.
- `registerEsFiles`, `mergeEsOnOS` — event-service output registration
  and merging on object storage.
- `altStageOut` — enables or forces the alternative stage-out
  mechanism at a queue.
- `stayOutputOnSite`, `onSiteMerging`, `instantiateTmplSite` — output
  locality and per-queue dataset instantiation.
- `ddmBackEnd` — names the DDM backend.
- `--destSE`, `--spaceToken` — destination storage element and space
  token from the client.

### Input without Rucio

The output and log legs of a Rucio-free workflow are demonstrated; the
input leg is the remaining one, and the mechanisms exist:

- `pfnList` (`--pfnList`) — input specified as a list of physical file
  names, explicitly supporting files unregistered in DDM. This is the
  direct route to feeding workers from an object store or plain URLs.
- `writeInputToFile` (`--writeInputToTxt`) — the job receives its input
  list as a file rather than on the command line.
- `useLocalIO` — input always copied to scratch rather than read
  remotely; the correct mode for a worker with no storage nearby.
- `allowInputLAN`, `allowInputWAN` — permit direct reads over LAN or
  WAN.
- `usePrefetcher`, `useZipToPin`, `inputPreStaging` — prefetching, zip
  pinning, and data-carousel pre-staging.
- `--inputFileList`, `--match`, `--antiMatch`, `--nSkipFiles`,
  `--useLogAsInput` — input selection.

### Payload delivery and parameter handling

- `encJobParams` — job parameters base64-encoded rather than passed as
  a quoted string.
- `noExecStrCnv` — the pilot skips execution-string conversion.

  Together these address the parameter-quoting handling that motivates
  part of the `epicrun` executor (`tools/worker/epicrun.py`), and
  should be evaluated before that executor's interface is settled.

- `useBuild` (`--noBuild`, `--noCompile`), `--inTarBall`,
  `--outTarBall`, `--tarBallViaDDM`, `--extFile`, `--workDir`,
  `--useHomeDir` — sandbox construction and delivery. Relevant because
  the present GPU payload clones its source from GitHub at run time,
  which makes every job depend on external network reachability from
  the worker; a delivered tarball removes that dependency.
- `usePrePro`, `multiStepExec` — pre-processing and multi-step
  execution.
- `--alrb`, `--alrbArgs`, `--wrapExecInContainer`, `--oldContMode`,
  `--containerImage`, `--architecture` — container execution modes.
  The pilot containerizes on the job's image field; these options are
  the vocabulary for the alternatives.
- `--execWithRealFileNames`, `useFileAsSourceLFN`,
  `addNthFieldToLFN`, `--descriptionInLFN` — naming behaviour.

### Fine-grained and streaming dispatch

The streaming model in the plan — bounded work quanta, re-queue on
worker loss — maps onto existing machinery rather than new
construction:

- `fineGrainedProc` — jobs track processing through the event service
  mechanism.
- `segmentedWork`, `dynamicNumEvents`, `nEventsPerWorker`,
  `maxEventsPerJob`, `tgtNumEventsPerJob`, `nEventsPerInput` — work
  segmentation and quantum sizing.
- `nEsConsumers`, `nSitesPerJob`, `useJobCloning`,
  `resurrectConsumers`, `switchEStoNormal` — consumer topology and
  recovery.
- `maxAttemptES`, `maxAttemptEsJob`, `decAttOnFailedES`,
  `notDiscardEvents` — retry policy at event-range granularity, which
  is the loss policy for a worker that disappears.
- `nJumboJobs`, `maxJumboPerSite` — jumbo jobs across event ranges.
- `runUntilClosed` — a task that continues until its input is closed:
  the steady-state consumer shape.
- `Client.get_events_status`, `Client.update_events` — the quantum
  ledger, readable and writable from the client.
- `randomSeed`, `firstEvent`, `useRealNumEvents`, `inFilePosEvtNum` —
  seeding and event numbering, required for simulation workloads split
  across many workers.

### Brokerage for a heterogeneous fleet

Relevant to driver and CUDA inhomogeneity across contributed hardware,
the sharpest open technical question for the volunteer track:

- `--architecture` — base OS platform, CPU and GPU requirements,
  applied at brokerage.
- `osMatching`, `ipConnectivity`, `ipStack`, `avoidVP`, `limitedSites`,
  `--site`, `--excludedSite` — matching by operating system, network
  class, and queue.
- `t1Weight`, `fullChain`, `intermediateTask`, `onlyTagsForFC` —
  nucleus and chain assignment.

### Tolerating unreliable workers

Volunteer machines are slow, shared, and interrupted. The relevant
controls exist:

- `minCpuEfficiency`, `useExhausted` — efficiency threshold and
  transition to exhausted rather than failure.
- `maxWalltime`, `--cpuTimePerEvent`, `--fixedCpuTime`,
  `--memory`, `--fixedRamCount`, `maxCoreCount`, `--maxCore` —
  resource declarations and ceilings.
- `noLoopingCheck` — disables looping-job detection, which
  misclassifies long GPU work with little I/O.
- `retryRamOffset`, `retryRamStep`, `retryRamMax`, `retryModuleRules`
  — retry policy and its parameter adjustments.
- `disableAutoRetry`, `disableReassign`, `disableAutoFinish`,
  `noAutoPause`, `allowPartialFinish`, `allowEmptyInput`,
  `failGoalUnreached`, `--allowNoOutput` — completion and retry
  policy, including outputs that are legitimately absent.
- `useScout`, `scoutSuccessRate`, `respectSplitRule` — scout job
  behaviour; the present GPU tasks set `skipScout`.

### Scale and throughput

- `--bulkSubmission` with `--inOutDsJson` — bulk task submission,
  the shape for many small tasks across a fleet.
- `totNumJobs`, `maxNumJobs`, `nMaxFilesPerJob`, `nFilesPerJob`,
  `nGBPerJob`, `noInputPooling`, `nChunksToWait` — job counts and
  splitting.
- `pushJob` — jobs pushed to the pilot through the message broker
  rather than pulled on the pilot's polling cycle. Directly relevant
  to dispatch latency, which is the dominant term in the current
  end-to-end time for a single job.
- `--express` — express quota for higher priority.
- `mergeOutput`, `--mergeLog`, `--mergeScript`, `nGBPerMergeJob`,
  `nEventsPerMergeJob`, `nFilesPerMergeJob`, `nMaxFilesPerMergeJob` —
  output merging.

### Operations and diagnostics

- `Client.setDebugMode` — turns debug mode on for a **running** job,
  streaming its stdout to the monitor. Suited to diagnosing a remote
  worker without waiting for a log round trip.
- `debugMode` (`--debugMode`) — the same at task level.
- `Client.killJobs`, `Client.killTask`, `Client.finishTask`,
  `Client.retryTask`, `Client.reactivateTask`, `Client.resumeTask`,
  `Client.pauseTask`, `Client.increase_attempt_nr`,
  `Client.reload_input` — task and job operations, exercised for the
  production task-operations work.
- `Client.send_file_recovery_request` — file recovery.
- `--allowTaskDuplication`, `--skipFilesUsedBy`, `--useNewCode` —
  resubmission semantics against the same output dataset.

### Query and monitoring API

Backing material for the monitoring worklist below:

- `Client.getUserJobMetadata` — metadata reported by the payload,
  retrievable per task.
- `Client.get_files_in_datasets` — server-side file listing for a
  task; a candidate backend for file-list download on the task and
  data-finder pages.
- `Client.requestEventPicking` — builds a dataset from specified runs
  and events; a candidate backend for loading a small number of events
  from a running task into an event display.
- `Client.getJediTaskDetails`, `Client.get_task_details_json`,
  `Client.get_job_descriptions`, `Client.getFullJobStatus`,
  `Client.getPandaIDsWithTaskID`, `Client.get_parent_detailed_info`,
  `Client.getJobIDsJediTasksInTimeRange`,
  `Client.get_tasks_detailed_info_since` — task and job detail
  retrieval.
- `Client.getTaskParamsMap`, `--dumpTaskParams`, `--dumpJson`,
  `--loadJson`, `--loadXML` — retrieve or supply a full parameter map.
  This supplies working exemplars for any workflow shape without
  database queries.
- `--noSubmit` — build a submission without sending it; useful when
  developing a submitter.

### Workflows

- `--parentTaskID` — run a task concurrently with its parent.
- `Client.call_idds_command`, `Client.call_idds_user_workflow_command`,
  `Client.submit_workflow`, `Client.send_workflow_request` — iDDS and
  PanDA native workflow submission.
- `hpoWorkflow`, `loadXML`, `workflowHoldup` — hyperparameter
  optimization and workflow control.

## Documentation map

The PanDA documentation carries subsystem chapters beyond the task
parameter reference. Those bearing on planned work: Working with iDDS;
Working with PanDA Native Workflows; the Messaging Mechanism (behind
`pushJob` and `messageDriven`); the Job Retry Module
(`retryModuleRules`); Brokerage; Job Sizing; Dynamic Optimization of
Task Parameters; Computing Resource Allocations; Site and Task
Classification; JEDI Watchdogs; Data Carousel; System Configuration
Parameters in Database; Integration with CRIC; Deployment of Custom
IAM; PanDA Daemon; System Architecture; Database; Installation.

PanDA also ships an MCP server ("Enabling PandaMCP"; `pandamcp/` is
present in the deployed server tree). Its tool set should be reviewed
before building overlapping tooling.

## Suggested probe order

1. `useSecrets` with `Client.set_user_secret` — removes object-store
   credentials from the worker using machinery that already exists,
   and is the largest single de-privileging step available without new
   infrastructure.
2. `putLogToOS` and `registerDatasets` — establish whether the
   object-store log path has a native task-level form, which would
   simplify the queue configuration currently carrying it.
3. `encJobParams` with `noExecStrCnv` — determine how much of the
   parameter-quoting handling they remove before settling the
   `epicrun` interface.
4. `pfnList` — the input leg of a Rucio-free workflow.
5. `pushJob` — dispatch latency, measured against the current polling
   cycle.

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
   and driver version; and the payload result — for the optical photon
   test, photons propagated, hits stored, GPU time per event, and the
   pass or fail verdict. Every one of these facts is present in the
   payload log and none is in the PanDA schema.
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

Two further page features have backends in the client API rather than
in new services: file-list download from the task and data-finder
pages (`Client.get_files_in_datasets`), and loading selected events
from a running task into an event display
(`Client.requestEventPicking`).

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
