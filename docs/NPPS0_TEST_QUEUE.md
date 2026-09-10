# BNL_NPPS_GPU as the pilot and payload test queue

`BNL_NPPS_GPU`, the PanDA queue on the NPPS GPU server npps0
([NPPS0_WORKER.md](NPPS0_WORKER.md)), is the test queue for the pilot
and payload infrastructure: pilot builds and the pilot's ePIC user
module, the pilot environment, the queue's pilot-side configuration,
container images, credentials, stage-out routes, the runner and the
payload, and the Event Service path the node event dispatcher builds
on. Every piece of that chain is under production operations control
on the host, and the jobs are real PanDA jobs against the production
server, with their records on the production monitoring pages. A
change is a commit, a file copy to the host, and a job: no harvester,
no compute element, no site operator, no queue wait.

The queue is not the host's only occupant. npps0 is the GPU
development machine and carries other work outside the queue. The
queue's share is the job shape in the queue record: 8 cores, 48 GB
(`maxrss`), 30 GB of working directory (`maxwdir`) and 24 hours
(`maxtime`) per job.

## What the queue provides

| Piece | Where it is set |
|---|---|
| The pilot | `--piloturl` on the pass script's wrapper invocation (below) |
| The pilot environment | the pass script, `tools/npps0/epicprod-gpu-pilot.sh`: credentials, Rucio, the S3 profile, and any pilot control variable such as `PILOT_ES_EXECUTOR_TYPE` |
| The queue's pilot-side behavior | `tools/npps0/config/queuedata.json`, applied per pass; CRIC holds the queue's existence only |
| The storage catalog | `tools/npps0/config/agis_ddmendpoints.json`, applied per pass |
| Container images | CVMFS unpacked images named by the job, run with `--nv` |
| Credentials | the PanDA token, the Rucio proxy, the JLab EVGEN proxy and the S3 profile, held on the host |
| Stage-out | the devcloud S3 bucket for logs ([DEVCLOUD_STAGEOUT.md](DEVCLOUD_STAGEOUT.md)); JLab Rucio for science outputs, on the payload data path |
| The payload | the epicprod payload in the submission sandbox ([EPICPROD_PAYLOAD.md](EPICPROD_PAYLOAD.md)); the `epicrun` executor (`tools/worker/epicrun.py`, [PANDA_CAPABILITIES.md](PANDA_CAPABILITIES.md)) |
| Dispatch | pull mode: the pilot asks the server for a job at every pass, so a job submitted to the queue starts within one pass cycle |

## Choosing the pilot

The BNL pilot wrapper the pass script runs from CVMFS resolves the
pilot tarball in this order: the `--piloturl` argument when one is
given; otherwise `pilot3-<version>.tar.gz` from the production pilot
directory when a version is named, and `pilot3.tar.gz` there by
default. The URL is fetched with curl, so any scheme curl serves is
admissible, `file://` included; `--piloturl local` skips the fetch and
extracts a `pilot3.tar.gz` already present in the run directory. The
pass script sets the URL in one variable, `PILOTURL`, and passes it
as `--piloturl`.

Three pilots are therefore one line apart:

- the production default: `PILOTURL` empty;
- the canary pilot, the setting in the script:
  `file:///cvmfs/eic.opensciencegrid.org/panda/pilot/canary/pilot3.tar.gz`,
  or a named tarball in that directory: the pilot production
  operations publishes there ([OSG_SUBMISSION.md](OSG_SUBMISSION.md)
  § Our canary pilot directory). This queue is the recorded consumer
  of that directory;
- a pilot built by hand: `PILOTURL=local`, with the tarball placed as
  `pilot3.tar.gz` in the host's configuration directory, from which
  the pass script copies it into each run directory. No publication,
  for iterating on a change before it goes to the canary directory.

`PILOTVERSION` inside a patched tarball stays the base version; the
tarball name carries the patch.

## The program

Each step runs here before it is asked of any other queue. In order:

1. **The canary pilot on the queue.** The pass script names the canary
   pilot, and an ordinary job on the queue confirms that the pilot
   runs, stages and reports as before. This is the first queue to run
   `pilot3-3.14.3.3-epic1`, the 3.14.3.3 release with pilot3 PR 220
   ([NODE_EVENT_DISPATCHER.md](NODE_EVENT_DISPATCHER.md)).
2. **The Event Service pilot path.** An Event Service task at the
   queue (the probe kit in swf-monitor `scripts/es-probe/`, a small
   task with `nEventsPerWorker` set) exercises the pilot's
   event-service path for the first time on any queue: the executor
   chosen (`PILOT_ES_EXECUTOR_TYPE` in the pass script's environment;
   the executor is the site's choice, not the task's), the range
   channel it opens, what the payload is asked to speak, and the range
   dispositions reported to the server. The channel library the
   generic executor needs (yampl) is absent from every pilot install;
   on this host it can be installed, so the generic executor can be
   tried alongside the alternatives.
3. **The pilot/harness boundary, settled in house.** Both shapes of
   the boundary ([NODE_EVENT_DISPATCHER.md](NODE_EVENT_DISPATCHER.md)
   § Open questions) can be built and run on this host: an ePIC
   process class in the pilot speaking the harness inbox/outbox
   contract, or the harness speaking the pilot's range channel. A
   pilot carrying either is fed to the pass script as a local tarball,
   so an iteration costs no publication. The rule stands: resolve
   maximally in the payload and the ePIC user module, with the
   smallest change to the pilot core.
4. **The node harness as real jobs.** The range-form unit spec, the
   simulation contract executable, the N-worker driver and the rolling
   zip with 30-minute closes registered to JLab Rucio
   ([NODE_EVENT_DISPATCHER.md](NODE_EVENT_DISPATCHER.md)
   § Implementation basis) run as Event Service jobs on the queue.
   Whether the server accepts range-finished reports without attached
   zip records is answered here against the live server. Deferral is
   tested by killing a worker mid-range and by a synthetic deadline far
   shorter than the four-hour allocation wall it stands in for; the
   deadline-and-drain logic is the same at ten minutes as at four
   hours.
5. **Physics per range.** npsim and eicrecon on EVGEN ranges, up to 8
   workers, outputs registered and counted per range: the exact-events
   accounting tier of [CAMPAIGN_DELIVERY.md](CAMPAIGN_DELIVERY.md),
   end to end.
6. **Job-level prmon from the runner.** The payload measures every
   stage with the image's prmon (`/opt/local/bin/prmon`, prmon 3.2.0
   in `eic_xl` 26.07.1) and reports per stage
   ([EPICPROD_PAYLOAD.md](EPICPROD_PAYLOAD.md) § The payload report).
   The pilot's own job-level monitor never starts on ePIC jobs: the
   ePIC user module's setup for it (`pilot/user/epic/utilities.py`,
   `get_memory_monitor_setup`) prefixes the command with
   `lsetup prmon;`, an ATLAS setup step that does not exist in these
   jobs, so the pilot's memory accounting and its over-memory handling
   fall back to what it can see from outside. The pilot does not need
   to have launched the monitor to use its output: at every job update
   it reads `memory_monitor_output.txt` and
   `memory_monitor_summary.json` from the job directory by name
   (`get_memory_monitor_info`) and lifts the Max and Avg fields into
   the job record, and the over-memory path reads the same output. The
   fix is in the runner: `epicrun` wraps the payload in the image's
   prmon, writing those two files into the job directory, one level
   above the container's work directory, where the runner already
   places declared outputs. The pilot then picks them up as if it had
   started the monitor, and its own failed launch remains a warning in
   the log. The verification on this queue: `maxpss` and its
   companions on the job page from measurement rather than the cgroup
   fallback, and an over-memory job ending with the prmon error rather
   than the cgroup kill. A correction of the user module's own hook is
   optional and later.
7. **Canary probes on the canary pilot.** site-canary probes at the
   queue under the canary pilot, the directory's original purpose.

The same rule covers the rest of the in-job path as it evolves:
`epicrun` as the `runGen` replacement, the work-unit contract
([WORK_UNIT_CONTRACT.md](WORK_UNIT_CONTRACT.md)), stage-out routes,
the job report ([JOB_REPORTING.md](JOB_REPORTING.md)), and the
pending-registration path ([RUCIO_RESILIENCE.md](RUCIO_RESILIENCE.md)).
Each is a job on this queue before it is a job anywhere else, and each
becomes production behavior when the same sandbox or container reaches
the production queues.

## What the queue cannot represent

These remain for the OSG queues and Perlmutter:

- The glidein layer and pool heterogeneity: the pool's default image,
  container nesting, and the site-by-site differences in CVMFS and
  user namespaces ([OSG_SUBMISSION.md](OSG_SUBMISSION.md)).
- Push-mode dispatch through harvester, and the harvester and Globus
  Compute worker form at NERSC.
- The node-exclusion levers, which live in the OSG submit description.
- Wide-area transfer behavior. Lab dCache is unreachable from npps0 by
  network position, so its stage-out is S3 and JLab Rucio; the BNL
  disk routes are not exercised.
- The four-hour Slurm wall and its allocation shape. The wall is
  simulated by a short deadline, not reproduced.
- The NERSC environment: the site's own harvester installation, the
  pilot it runs, and the software area it points into. The canary
  pilot does not reach Perlmutter through the queue record alone
  ([OSG_SUBMISSION.md](OSG_SUBMISSION.md)).

The Perlmutter specifics are therefore the last step, taken to the
site's PanDA operations as one concrete ask once the design has run
here. The event-service design itself is settled on this queue.

## Deploying a change

Scripts and configuration are files on the host, read fresh at every
pass; a change takes effect at the next pass boundary with no service
action ([NPPS0_WORKER.md](NPPS0_WORKER.md) § Deploying a change). A
pass holding a job is never killed.

## Related

- [NPPS0_WORKER.md](NPPS0_WORKER.md): the host, the launcher and pass
  scripts, the git-sourced configuration, the credentials.
- [NODE_EVENT_DISPATCHER.md](NODE_EVENT_DISPATCHER.md): the design the
  Event Service steps above prove.
- [OSG_SUBMISSION.md](OSG_SUBMISSION.md): pilot selection, the canary
  pilot directory and publishing to it; the OSG levers this queue does
  not represent.
- [EPICPROD_PAYLOAD.md](EPICPROD_PAYLOAD.md): the payload and its
  report.
- [VOLUNTEER_GPU_PLAN.md](VOLUNTEER_GPU_PLAN.md): the queue's other
  role, the first worker of the volunteer-class GPU pool.
- [PANDA_CAPABILITIES.md](PANDA_CAPABILITIES.md): the PanDA mechanisms
  available to GPU worker jobs, and `epicrun`.
- site-canary `docs/PLAN.md`: the probes.
