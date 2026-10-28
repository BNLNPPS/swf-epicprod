# NERSC Perlmutter: how pilots start, and the Event Service there

Perlmutter runs ePIC production through a harvester operated by PanDA
operations on the site's login node. This document records how a pilot
comes to run on a Perlmutter node, what of that path is reachable from
the production system, the arrangement under which production operations
controls the pilot on its own Perlmutter test queue, and how an Event
Service (ES) job runs there. The ES design is
[NODE_EVENT_DISPATCHER.md](NODE_EVENT_DISPATCHER.md); the payload's ES
machinery is in [EPICPROD_PAYLOAD.md](EPICPROD_PAYLOAD.md) (Resident
particle-gun simulation, Resident Event Service units, Shared resident
reconstruction, Asynchronous reconstruction); the proving program that
precedes Perlmutter is [NPPS0_TEST_QUEUE.md](NPPS0_TEST_QUEUE.md).

## The queues

`NERSC_Perlmutter_epic_es` runs Event Service production on one node
(Production, below). The ordinary production queue is closed while the m3763
CPU allocation is above its close threshold (90% used), and has been off on
Torre's word since 26 October ("ES is our efficient means of using perl now");
the ES queue has its own floor instead and closes at 2,656 CPU node-hours left
(`nersc.close_at_left_h`).

| Queue | Role | Record |
|---|---|---|
| `NERSC_Perlmutter_epic` | production | `jobseed` `std`; mcore; `maxtime` 14400 s |
| `NERSC_Perlmutter_epic_es` | Event Service production on one node; production operations' launcher | created 2026-09-10 as a clone of `NERSC_Perlmutter_epic_dev`; `jobseed` `all`; mcore; `apptainer:pilot` with `/global`, `/cvmfs` and `/pscratch` bound; rucio copytool to `BNL_PROD_DISK_1`; `maxtime` 345600 s; ordinary jobs run on it |

`jobseed` `all` is the field that lets the server create a new consumer
for a task's unprocessed ranges (NODE_EVENT_DISPATCHER.md, The pilot
side); the production queue keeps `std` until the worker-shape change.

## The original site wrapper

Read from the worker record of a probe job on 2026-09-12 (job 2723589,
worker 20654, Slurm 58243510). The harvester submits a Slurm job in the
`m3763` allocation on one 256-core node. The job script copies the
wrapper `wrapper-wrapper-3-epic-test.sh` from the harvester
installation (`/global/common/software/m3763/panda-harvester/etc/panda/`)
into the worker directory on `/pscratch` and runs it under
`srun -n 80 <wrapper> <queue> <accessPoint>`: eighty single-core pilots
per node, each with `HARVESTER_ID`, `HARVESTER_WORKER_ID` and `GTAG` in
its environment. Each task sets up ALRB from `/cvmfs/atlas.cern.ch`
(`lsetup python pilot-default-SL9`, rucio, xrootd, davix, psutil,
logstash, prmon), writes an environment file and a payload script, and
starts an apptainer AlmaLinux 9 container from the ATLAS container
repository with `/global`, `/pscratch` and `/cvmfs` bound. Inside it the
pilot runs as

```
python3 <harvester installation>/pilot/pilot3-<version>/pilot3/pilot.py \
  -q <queue> -i PR -j managed -w generic \
  --url https://pandaserver01.sdcc.bnl.gov -p 25443 \
  --queuedata-url http://pandaserver01.sdcc.bnl.gov:25080/cache/schedconfig/<queue>.all.json \
  --pilot-user epic --allow-same-user=False --job-type=managed \
  --use-rucio-traces False --rucio-host https://nprucio01.sdcc.bnl.gov:443 \
  --getjobrequests=50 --cleanup True --noworkerpilotstatusupdate --debug
```

The pilot is an unpacked release directory in the harvester
installation, and the version is a variable in the wrapper (3.14.3.3 on
2026-09-12). CVMFS plays no part in delivering it. The wrapper, the
Slurm template and the harvester queue configuration are not in a
repository that production operations can find; a change to any of
them is a request to PanDA operations with the change spelled out,
and the request below asks that they be kept in one.

## Worker records

`https://portal.nersc.gov/cfs/m3763/panda/jobs/<queue>/launch/<worker>/task0.out`
serves, without login, the public launch record of a test-queue worker:
the launcher's own messages, the pool sample, the native runtime and pilot
checksums, the pilot's exit code, and the pilot's end-of-job step lines
(The end of a job). The pilot's console and the payload's logs stay in the
private worker directory. The per-job directories
`https://portal.nersc.gov/cfs/m3763/panda/jobs/<queue>/<pandaid>/`, which
served the site wrapper's pilot log and task output in September, are not
published for the test queue's native workers; `panda_study_job` lists the
harvester's log locations whether or not they are served.

## CVMFS on the nodes

The canary fingerprint, taken on every probe landing, reports
`eic.opensciencegrid.org` unreachable on Perlmutter nodes and
`singularity.opensciencegrid.org` reachable; the worker record shows
`atlas.cern.ch` and `oasis.opensciencegrid.org` in use. The canary
pilot directory in `eic.opensciencegrid.org`
([OSG_SUBMISSION.md](OSG_SUBMISSION.md) § Our canary pilot directory)
therefore does not reach Perlmutter: a pilot for Perlmutter is
delivered by file or over HTTPS. Outbound HTTP and HTTPS from the
compute nodes work; the pilot's own traffic to the PanDA server and to
Rucio proves it.

## Pilot-side queue configuration without a CRIC edit

The ePIC pilot user module reads queue data in the order LOCAL, PANDA,
CVMFS, CRIC (`pilot/user/epic/setup.py`): a `queuedata.json` in the
pilot's working directory is used first and without an age check
(`pilot/info/dataloader.py`); `--queuedata-url` is the PANDA source.
A file placed in the working directory before the pilot starts
therefore governs every pilot-side field, as it does on `BNL_NPPS_GPU`
and in the OSG submit description. The fields the server reads
(`jobseed`, `corecount`, `capability`, `resource_type`, `maxtime`,
`status`) stay in CRIC. The launcher fetches the test queue's file,
`perlmutter/NERSC_Perlmutter_epic_es/queuedata.json`, from `main`.

For the Event Service the file must declare the `es_events` and
`es_failover` activities, in `acopytools` as `rucio` and in `astorages`
as `BNL_PROD_DISK_1`, the route the queue's ordinary outputs take.
Undeclared, the pilot's event service stage-out forces the
`objectstore` copytool (`pilot/api/es_data.py`) and fails against a
storage that is not an object store. Its `maxwdir` is the pilot's limit on
the job's work directory, plus 20%: a job above it is killed with its pilot
(The end of a job). It is 150,000 MB since 26 October (30,000 before): with
gun output, four concurrent closes each hold their unit files and their
merged file, about 50 to 60 GB together.

The pilot's report interval (`zip_time_gap`) is 300 seconds. The effective
value must be checked through the pilot's full information-service
resolution, where its configuration provider and the `zip_time_gap` alias
compete; the batch pilot sets both names and verifies an effective
300-second interval.

## Controlling the pilot on the test queue

Production operations owns the pilot launch that runs on
`NERSC_Perlmutter_epic_es`: the script that prepares what a pilot needs
on the node and starts it. It lives in this repository at
`perlmutter/NERSC_Perlmutter_epic_es/epicprod-perlmutter-pilot-launch.sh`,
named to travel alone (a cached copy in the site's tree, a copy in
every worker directory): whose it is, where it runs, what it does. It
is served from `main`, as everything in this repository is: `main` is what
runs.

The site's Slurm job takes it by queue name, keeping a cached copy in
the harvester installation, where the job script now copies the site's
own wrapper:

```
OURS=/global/common/software/m3763/panda-harvester/etc/panda/queues/${PQ}/epicprod-perlmutter-pilot-launch.sh
mkdir -p "$(dirname "$OURS")"
curl -sfL "https://raw.githubusercontent.com/BNLNPPS/swf-epicprod/main/perlmutter/${PQ}/epicprod-perlmutter-pilot-launch.sh" -o "$OURS.new" && mv -f "$OURS.new" "$OURS"
cp "$OURS" epicprod-perlmutter-pilot-launch.sh 2>/dev/null || cp /global/common/software/m3763/panda-harvester/etc/panda/wrapper-wrapper-3-epic-test.sh epicprod-perlmutter-pilot-launch.sh
```

and the `srun` line runs `./epicprod-perlmutter-pilot-launch.sh` in place of
the site's file name. PanDA operations changed the `NERSC_Perlmutter_epic_es`
Slurm script on 2026-09-13 to fetch the launcher from its fixed URL (not the
`${PQ}` form, so the production queue keeps the site's wrapper).

The scope is set by the URL: a queue has a cached copy only if a file
is published under its name, so only `NERSC_Perlmutter_epic_es` runs
production operations' wrapper; every other queue's fetch fails and
the site's wrapper runs as before. On the test queue a successful fetch
replaces the cached copy atomically; a failed fetch leaves it, and the
worker runs the last good version. Once a queue has a cached copy, removing
the file from the repository does not return the queue to the site's
wrapper; every change in either direction is a publication on `main`. The
standing request to PanDA operations is that the Perlmutter harvester
configuration, Slurm template and wrapper be kept in a repository, so that a
change like this one is a pull request.

### The launcher and the native pilot

The launcher is a site adapter to the **common native pilot**. It starts the
campaign image's own Python through Apptainer, without ALRB; the Event Service
uses that pilot's generic executor and a matching Python 3.13 yampl channel.
The runtime and channel arrive over immutable, checksum-pinned HTTPS URLs under
the devcloud bucket's public `pilot/` prefix, since the EIC CVMFS repository is
unavailable on these nodes; a failed artifact check, or a runtime manifest
without the batch channel's declaration (`es_batch_channel` version 1, a
300-second report interval), ends the launch before dispatch, never falling
back to another pilot.

- **Worker shape.** The Slurm script still starts eighty copies of the
  adapter; only rank zero continues, producing one pilot on the single-node
  allocation. The pilot advertises 128 physical payload cores
  (`ATHENA_PROC_NUMBER`), keeps the Harvester identity and uses a mode-0600
  copy of the site's proxy. A worker takes at most one job and then exits.
- **Lifetime.** The worker's ceiling is four hours including staging,
  clamped to an earlier Slurm end time; HEPiX `shutdowntime` exposes the same
  deadline to the pilot and to the ES harness. The pilot stops the job once
  less than ten minutes remain (checked once a minute), so the harness ends,
  with its last close and reports, 13 minutes before the shutdown
  (`es_harness.py`, `PILOT_SHUTDOWN_GRACE_S`, `FINAL_REPORT_S`, payload
  0.25.23). Task 41262 planned its end 4 minutes before the shutdown; the
  pilot stopped it 6 minutes into its drain, and its last closes went
  uncredited. The pilot's first-job admission minimum is ten minutes.
- **Identity.** The adapter resolves the scheduler's uid/gid on the host and
  stages private passwd/group lookup files before entering a user and mount
  namespace; they are bound over the lookup paths only in that namespace, so
  the CVMFS Apptainer resolves the account without the site's NSS service. A
  failed host lookup stops the launch before dispatch.
- **Pool sample.** Before the container starts, the adapter samples `squeue`
  and `sinfo` into `pool-sample.json` (`pool-sample/1`), exported as
  `EPICPROD_POOL_SAMPLE`, so the payload report carries the account's and
  machine's running and pending jobs and partition state (swf-monitor
  `docs/POOL_REPORTER.md`). The node-exclusion check runs before pilot
  startup.
- **Staggered starts.** Nodes granted together would start their payloads
  together and load the storage doors in step, whichever path submitted them
  (the feeder, a trial, a test). Before the pilot starts, each node takes the
  next start slot, at least 300 s (`START_GAP_S`) after the previous node's,
  from a record on the community file system under a mkdir lock, and waits for
  it; a node alone starts at once. The record is public at
  `https://portal.nersc.gov/cfs/m3763/panda/start-slots/last`, and each launch
  record states the slot it took. A record that cannot be used is stated and
  the node starts at once: the stagger relieves pressure and never stops a
  launch.
- **Admission.** Image admission reads the same public launcher URL through
  the cached product `perlmutter_pilot_runtime` (five-minute refresh,
  fifteen-minute maximum evidence age) and derives the exact image and pinned
  runtime hashes from it; missing, unreadable, ambiguous or stale evidence
  refuses submission. The product refreshes only while something long-lived
  reads it: before a submission after a quiet interval, it is refreshed
  (`perlmutter_pilots.capability(refresh=True)`, the page's Update).

### The batch pilot

The runtime is `native-runtime-perlmutter-es-batch-50969f0`, pilot archive
`03b435bb18508227d4c9f4b18b6d8f9cd35dae5d98a52a4c4886e7d993d22733`, built
from the approved native archive `9532f30b` by
`scripts/build-native-es-batch.py`, staged by `scripts/stage-native-runtime.py`
from committed helpers and frozen assets, and published by
`scripts/publish-pilot-asset.py`, which never overwrites a key and verifies
the public copy's checksum. Google's batch runtime is built from the same
module and builder ([GKE_PILOT_FLOW.md](GKE_PILOT_FLOW.md)). The batch
behaviour lives in `pilot/native-canary/es_batch.py`, gated by
`EPICPROD_SWITCH_ES_BATCH_CHANNEL=1`, which the launcher exports:

- **Ranges in bulk.** One IPC request takes up to 512 ranges from the pilot's
  cache; an empty cache is refilled by one server acquisition of eight ranges
  per core, at most one every two seconds (`es_batch.acquire_count`), so up to
  about 1,000 ranges a second on 256 cores. One per core held the first
  production job (6071469) to about 100 a second: 5-event units, a fifth of
  the rate of 128 slots of 0.26 s gun events. Eight per core still left job
  6074509 (task 41299) at about 250 a second: the server answered each
  2,048-range acquisition in 0.2 s, contiguous, but the pilot refills only an
  empty cache and answered 256-range requests about once a second, so 104 of
  its 128 slots stood idle and units ran a median 9 events against the 440
  events a second its slots simulate (0.289 s each). The harness asks for
  512 a request from payload 0.25.28, the batch pilot's limit. From 0.25.29
  the record's `channel` carries the requests, the ranges and the pilot's
  answer times (median, 90th percentile, longest of the latest 500), which
  tell a slow pilot from a harness that asks too slowly.
- **Ranges held from the start, reports at once.** The pilot's communication
  manager is one thread, a pass a second, carrying acquisitions, range reports
  and the job's updates in turn. A report of 10,000 ranges sent right behind
  another took the server 22 to 33 s (2,140 took 0.6 s), so each periodic
  stage-out's run of them held every acquisition for minutes: job 6095121
  (task 41299) alternated between about 400 events a second and 5- to
  15-minute windows of about 50, with acquisitions 4 s apart fed and 50 to 60 s
  apart starved, about 215 a second overall. The launcher sets
  `EPICPROD_ES_PREFETCH_EVENTS=200000`: the ES process fills a cache of ranges
  holding 200,000 events at its start, and a thread of its own refills it
  straight from the server, up to 10,000 ranges an acquisition, whenever they
  hold fewer than half of that, about seven minutes of a fed node. The size is
  in events, so ten-event ranges do not make it 2M events (es_batch 18e2802); the payload is answered from the cache, and "No more events"
  only when the server has none left.
- **PanDA restarts.** PanDA marks an acquisition's ranges sent to the job,
  committed, before it answers, and it restarts every night with its log
  rotation and at every upgrade, ending the requests in flight. A restart at
  12:17:32 ET on 27 October cut the answer to job 6252252's acquisition of
  events 90,002 to 100,001 (7,956 of about 2.2 MB sent); the ranges stayed sent
  to the job and unknown to its pilot until the job ended, and job 6254762 ran
  them. PanDA's record of the job's ranges is the state the pilot keeps to:
  after a failed acquisition the prefetch waits a minute, reads the job's
  ranges back (`get_event_range_statuses`), and takes every range sent to the
  job that it never received, rebuilt from its first event and the range size;
  production ES tasks therefore have events divisible by `range_events`. It
  answers "No more events" only after that reconciliation. Range reports are
  re-sent until the server takes them, so a restart costs an ES job a retry, as
  it costs an ordinary job.
- **Reports at once.** With
  `EPICPROD_ES_REPORT_AT_ONCE=1` the
  executor's pass, every five seconds, sends the queued unit reports straight
  to the server, 2,000 ranges a call, so a close is credited as it stands and
  the periodic receipt archive goes unused. Ranges a job holds when it ends are
  released by the server's fine-grained accounting.
- **One report per unit.** The harness reports a completed unit with all of
  its range IDs in one message (up to 512); the pilot packs each unit's record
  once into the stage-out archive and reports finished ranges through the
  normal bulk event API, at most 10,000 IDs per request.
- **The server's answer.** On PostgreSQL, panda-server commits a range
  update and then fails its own job event-count statement, appending one
  `False` to the per-range answers. The pilot accepts exactly that shape as
  committed; any other refusal raises. Before, the pilot re-marked already
  credited ranges failed (error 1220) and they were dispatched again, so the
  same events were simulated twice.
- **The final report.** The forced stage-out at the payload's end reports the
  last finished ranges straight to the server, without a receipt archive
  (`es_batch.report_now`). It must not go through the communication manager:
  on a stop the executor stops that manager first, so a stopping pilot's last
  reports were aborted and marked failed. The server takes a range update
  without a zip record, and fine-grained credit counts finished ranges.
- **Failed units.** The harness reported a failed unit with one message a
  range, and the pilot sent each to the server in a call of its own, a second
  or more, inside the loop that answers the payload's range requests. In job
  6254767 (task 41445, 27 October) one slot whose resident simulation had died
  failed 961 units of 500 one-event ranges, one after another, in no time each
  (EPICPROD_PAYLOAD.md, Resident Event Service units); the job's range requests
  waited behind their 480,000 messages, sent on at about 40 a minute, with its
  128 slots idle and the pilot's range cache full. The batch pilot now queues
  failed ranges for the executor's pass, which sends them in bulk, 2,000 a
  call, and tells the payload through `EPICPROD_SWITCH_ES_FAILED_UNIT` that it
  takes a failed unit as one message (payload 0.25.32). A failed report is
  bookkeeping only: the server releases a failed range to the next job with
  the job's other unfinished ranges. A payload message the pilot cannot handle
  is logged and its ranges left to the server; it no longer stops the ES
  process. The close-out recovers a node whose feed starves for any other
  reason (NODE_EVENT_DISPATCHER.md, Preemption).
- **Inputs staged at the site.** An input already at the site at its catalog
  size, in a directory the launcher names (`EPICPROD_SITE_INPUT_DIRS`, the CFS
  staging on Perlmutter), is linked into the job's directory and marked
  delivered before the transfer (`es_batch.link_site_staged`): the pilot asks
  no catalog for a replica of it, so a job's input may reside wherever its
  catalog says. Any other input goes to the ordinary transfer.
- **The job's log.** The ES executor leaves the pilot process in the job's work
  directory, and the pilot's `create_log` wrote the archive there, then renamed
  the directory away before tar ran, so no log was made and the stage-out
  failed with error 1165 (job 6258483; the reason ES tasks had no log dataset
  from September). The batch build puts the archive in the pilot's home, beside
  the renamed directory, and lets tar's exit status decide.
  Log test 41454 (job 6268857, runtime bd83375) staged a 5.3 MB log; the
  Perlmutter feeder's tasks stage theirs from 27 October (`job_log` in the
  production ES definition, which needs the batch channel).

## Event Service jobs on Perlmutter

One job occupies one node for its allocation and runs the node harness with
128 execution slots, one per physical core; the node's second hardware
threads carry reconstruction. The current shape, as submitted for the
four-hour runs:

| Setting | Value | Effect |
|---|---|---|
| ranges | fine-grained, one event per range | `--es-fine-grained` |
| unit | 100 events | `--es-events-per-range 100` |
| slots | 128 | `--es-slots 128` |
| closes | every 300 s, fast merge | `--es-close-s 300`, `EPICPROD_SWITCH_ES_FAST_MERGE=1` |
| deadline | 14,100 s, 600 s drain margin | `--es-deadline-s 14100 --es-margin-s 600` |
| memory request | 1,500 MB per core | `--es-ram-per-core-mb 1500` |
| simulation | one resident npsim per slot, gun or external EVGEN | `EPICPROD_SWITCH_RESIDENT_SIM=1`, `EPICPROD_SWITCH_RESIDENT_EVGEN=1` |
| units | later units straight to the residents | `EPICPROD_SWITCH_ES_RESIDENT_UNIT=1` |
| reconstruction | 32 shared resident EICrecon members, asynchronous | `EPICPROD_SWITCH_ES_RECO_POOL=32`, `EPICPROD_SWITCH_ES_RECO_ASYNC=1` |
| start | all slots together | `EPICPROD_SWITCH_ES_COHORT_START=1` |
| last reports | the pilot's receipt before the harness ends | `EPICPROD_SWITCH_ES_REPORT_BARRIER=1` |

Each slot's first unit runs the payload's `run.sh` and writes the slot's unit
template; later units are one request to the slot's resident simulation and a
handoff, with reconstruction finishing in the background through a free pool
member while the slot simulates its next unit. A close every 300 s merges the
handed-off units into one RECO file, registers it at its RSE, removes the unit
files and the merged file, and then reports the units' ranges to the pilot.
At the deadline minus the margin the harness takes no more ranges, cuts the
last units to what fits, makes the last close and waits for the pilot's
receipt of its last reports.

Memory sets the reconstruction layout. A slot's resident npsim holds about
1.9 GB and a resident EICrecon about 2.3 to 2.9 GB, so one of each per slot
needs about 560 GB on a node with 491 GB available to the job; task 41229
(`pES6`) had 8 GB available eleven minutes in. With 32 shared members the
node holds 125 to 140 GB available throughout (tasks 41249, 41256, 41262).

### Submitting, watching and reading back

An operator submits an ES task from swf-testbed with
`scripts/submit-es-canary.py --log <file> -- <submit-evgen-task.py arguments>`,
which runs the deployed submitter with the deployed environment, the
operator's own token and the internal monitor URL; the switches above go as
`--canary-switch` settings, which the harness reads from the sandbox's
environment file. Native ES canaries use `--es-worker-proxy`: the sandbox
names a local proxy path, and the dispatcher copies the native pilot's
credential into it on the worker with mode 0600, so the live credential is
not uploaded in the sandbox. Both `X509_USER_PROXY` and
`PILOT_X509_USER_PROXY` name that file in the sandbox environment, so the
closes, which start under `--cleanenv`, exchange the pilot identity for
PanDA-issued Rucio and storage write tokens.

A canary task makes one pass over its file: ranges its job leaves are not
given to a second job. A task may therefore offer more events than one
allocation can process, and the job works to its deadline. The monitor's
task Finish is PanDA's hard finish and cancels the running job, whose
fine-grained credit is then lost; it is not a way to end a canary early.

`scripts/es-observe.py --task <id> [--every <s>] [--evidence <dir>]` reads the
task's jobs and their shipped harness records, read-only. After the job,
`scripts/es-readback.py --dataset <DID> --record <record> --output <manifest>`
prepares the registered files, and `--validate <manifest> --evgen --source
<EVGEN file>`, run in the campaign image, reads every registered event with
the campaign's PODIO reader and checks each event ID once, every collection's
readability, and each event against the source event of the same number
(NODE_EVENT_DISPATCHER.md, Resident simulation with external EVGEN input).

### Measured

Task 41262 (job 6055183; 5x130 DIS EVGEN, the shape above, payload 0.25.18,
batch pilot `d1538119`):

- The 128 simulations started together reached their first events at a
  median 156 s after the harness started: container start, the first unit's
  payload stages, the npsim kernel (58 s) and Geant4's lazy first-run setup.
- After that, slot cores simulated 99.6% of the time. A slot's turnaround from
  one unit's last event to the next unit's first was 1.8 s at the median, at
  4.5 s an event for 100-event units; the node simulated 27.8 events/s.
- Reconstruction ran on the shared members, off the slot cores, at about
  0.5 s an event.
- Closes of 10,000 to 12,000 events merged and registered in about two
  minutes.

The same steady window measured 87.6% simulating with synchronous resident
units (task 41249) and 92.9% with asynchronous reconstruction before a slot
whose unit had simulated was fed as idle (task 41256). Every registered event
of 41249 (34,416) and 41256 (32,000) read back against its source event.

Over a four-hour allocation the one-time startup and the job's end are the
remaining costs: about 93% of allocated core time simulating, 94% with the
one initialization counted, before the job-end tail below is shortened.

## Production

On 26 October Torre put `NERSC_Perlmutter_epic_es` into production at one
node, running fresh particle-gun statistics as Google does (decision
`01a17817-55fd-7551-be68-d05dbb102492`). The queue is `production` in the queue
registry and in the front's queue list for normal PCS placement; its general
`front.queue.NERSC_Perlmutter_epic_es.feed` switch stays false. The m3763
balance closes it at 2,656 CPU node-hours left rather than at 90% used, and
reopens it above that: 6,000 node-hours were added on 26 October (7,656 left),
1,000 of them to be left to NERSC, so production uses 5,000 from then (Torre,
"5k from now"; 200 before). One node uses about 24 node-hours a day. The ES close-out covers it
(`es_closeout.queues`), so a job that loses its node is credited for its closes
that stood.

`swf_epicprod.perlmutter_production` runs at the end of each five-minute ES
close-out cycle. With `perlmutter_production.enabled=true` it adds a task only
when none of its own samples has a job or a live task on the queue, so
production holds one node; a paused task does not hold it. Tests and any other
tasks on the queue run beside production on nodes of their own and hold
nothing: the five-node target is soft, not a limit. Each task is 3,000,000 fresh 5 GeV
neutron-gun events at 3–50 degrees in one job, in the shape above with
500-event units and 120-second closes, four at a time, and from 27 October
ranges of ten events (`range_events`, JEDI's `nEventsPerFineGrainedRange`;
test task 41444 was credited 32,000 events in 3,200 such ranges), a tenth of
the range rows, server reports and job-insertion time of single-event ranges. On Perlmutter a
neutron simulates in 0.289 s with reconstruction running beside it (job
6074509, 6,337 units) and reconstructs in about 0.1 s (job 6071469), so a fed
node keeps about 45 reconstruction members busy: 48 shared members from 26
October (32 before, enough only for the starved 230 events a second of job
6074509). Its RECO holds 156 KB: the node writes about 69 MB/s, more than one
close's merge and registration carry (about 35 MB/s, task 41262), hence the
concurrent closes (`ES_CLOSE_CONCURRENCY`). One fed node makes about 1.6
million events and 250 GB of RECO an hour. A task that one job does not
finish continues in the next job on its remaining ranges.

The task size is bounded by job insertion. Inserting a job writes one
`JEDI_Events` row for every unprocessed event, in one transaction, and a
retry first cancels the rows of the job before; JEDI allows `storeJobs` 600
seconds, fixed in code (`pandajedi/jedicore/Interaction.py`). A first job of
6,000,000 events took 563 to 596 s (tasks 41287, 41299, 41316); retries took
464 s for 2,984,763 remaining events and 233 s for 82,747. The retries of
41287 (10,212 events credited) and 41316 (5,750,520 remaining) never fit: each attempt
timed out, was rolled back and was repeated ten minutes later, without
end, and the queue stood empty behind the live task. Tasks are therefore
3,000,000 events: about 300 s for a first job and at most about 400 s for a
retry, and a job that a fed node runs dry ends after about two hours.

PCS sample identities `pOP000001` onward share the seed ledger of Google's
feeder (GKE_PILOT_FLOW.md): every seed is disjoint from the source production
and from every ES sample on either queue, and each sample's generation plan is
registered the same way. Unknown PanDA state, or a paused, failed or
incomplete feeder task, holds replenishment for operator review; storage
headroom and a fresh read of the launcher's image apply before each
submission. The cached product `perlmutter_production` and the action records
of that name show each decision. Setting the switch false stops additions;
the running job drains.

### EVGEN input in production

Perlmutter takes no ordinary single-core work: a worker there is a whole node
for one job, so a 26.07 job, single-threaded, would hold 128 cores and use one
(Torre Wenaus, 2026-10-28: "NO MORE SINGLE CORE 1/128 EFF WORK IN PERL EVER").
`prodtask_submit_request` refuses a task without an Event Service shape for a
queue of a whole-node site (`swf_epicprod.queues.whole_node`, the NERSC
site), before any placement.

A PCS task over a registered EVGEN sample runs there through the Event Service
with its EVGEN files as the job's input, the shape of the DIS runs 41241 and
41262 (`swf_epicprod/evgen_es.py`):

- **Input.** The task's input is its registered sample's dataset in the
  catalog that holds it, JLab's Rucio today (`epic:/EVGEN/...`), limited to
  the files its manifest covers (`evgen_es.catalog_input`; JEDI's template
  `files`). Nothing is copied to or registered at BNL: JEDI reads the
  dataset, each file's size, adler32 and event count from that catalog
  through the ePIC DDM interface (`swf_epicprod.jedi.EpicDDMClient`,
  distcomp-services panda-server CHANGES.md, 28 October), which knows a
  path-named catalog's files by their names' last component, since JEDI's
  Event Service spec joins its fields with '/', and gives a file listed
  without a GUID a stable one from its `scope:name`. Perlmutter, which
  stages a task's whole input before the task is submitted, is reported
  holding the dataset's files on its local disk (the map's `site_staged`),
  so JEDI marks them ready and activates the job.
- **Shape.** One job per file, the pilot staging the job's own file so the
  payload simulates from it (`run.sh`, `EPICPROD_INPUT_LOCAL`), never from
  the door path of the manifest's first row; every unit records its source
  file. JEDI takes the files at their common event count
  (`nEventsPerInputFile`) and stops at the manifest's total (`nEvents`),
  both in whole ten-event ranges. It takes no file part way: a file whose
  count would pass the total is left out, with every file after it, so the
  file the manifest covers only part way runs as a task of its own, a later
  attempt of the same PCS task over the events the manifest takes from it
  (`evgen_es.tails`, the attempt's own setup in its `event_service`
  metadata). The front queues a task's tails with it;
  `scripts/evgen-es-tail.py <task>` queues those of a task already
  submitted. A job works to its four-hour deadline and the next continues the file's
  remaining ranges. The rest is 41262's shape: 128 slots, 100-event units,
  closes every 300 s, 32 shared reconstruction members, 1,500 MB a core,
  with the production gun's ten-event ranges, concurrent closes, job log and
  notice.
- **Outputs.** The task's own RECO dataset at JLab, in merged closes of
  thousands of events rather than one file a job; closes are named from the
  job's own EVGEN file with the job's PanDA id and close number
  (`EPICPROD_SWITCH_ES_INPUT_NAMES`), and each unit's record carries its source
  file.

The pressure front feeds the queue (its feed switch on): a whole-node queue
draws from the ready pool only tasks over a registered EVGEN sample
(`front.open_to`, the entry's `evgen_es` mark), and its feed stages the
task's input and sets its `event_service` shape before the submission
request (`front.feed_task`, `evgen_es.prepare`). The queue is sized in nodes,
not hours, since such a task is days of nodes (p4499, 14.5 million events:
about 150 node-hours). JEDI makes the task's jobs, one per file, at once
(41479: nine activated within two minutes), and in pull mode the harvester
gives each activated job a worker, a whole node, up to its worker ceiling on
the queue (distcomp-services perlmutter-harvester/CHANGES.md). The ceiling is
the node count; the front's `front.queue.<queue>.nodes` is set with it, and
the queue takes the next task while its own work fills fewer nodes than that:
its production jobs not started or running, its files JEDI has not made jobs
of, and at least one node for each live task (`front.decide`,
`node_occupied`). Jobs beyond the ceiling wait activated for a node; a test
beside production takes one of the ceiling's nodes.

A task brokered to the queue has its whole input staged at NERSC before it
starts (Torre Wenaus, 2026-10-28), so that no job pulls its file over the WAN:
the feed places the task on the queue, so no other queue takes it, starts one
copy of the files not yet there (`nersc_stage.ensure_staged`) and holds it,
`input_staging`; a later cycle submits it once every file is there at its
catalog size. The copy reads each file from the replica its catalog lists
(`nersc_stage.catalog_urls`, an available disk replica by the catalog's
priority, never a fixed door) into
`/global/cfs/cdirs/m3763/epicprod/esinput/<name's last component>`, checked
against its adler32 and renamed into place. It is a Slurm job in the transfer
QOS (cron architecture) submitted through the IRI API
(`swf_epicprod/nersc_iri.py`) under its account, `wguan`; it takes xrdcp from
ATLAS's tools on CVMFS (a transfer node has none of its own and no grid CA
directory) and reads with our production Rucio proxy, kept at NERSC in
`/global/homes/w/wguan/epicprod`, mode 700, the proxy mode 600, replaced
whenever ours is newer. A whole task copies in minutes: p4499's eight files,
28.6 GB and about 48 hours of three nodes, in about 2½ minutes (275 MB/s, 28
October). The job leaves its input to direct access (`nersc_input`), the
batch pilot links the site copy into the job and asks no catalog for a replica
(The batch pilot, Inputs staged at the site), and the harness reads it
(`EPICPROD_SWITCH_ES_NERSC_INPUT`). `scripts/evgen-es-nersc-stage.py stage
<task>` and `stage-dataset <did>` copy by hand the same way.

`scripts/evgen-es-submit.py <task> --by NAME --reason TEXT` submits a task by
hand the same way (copy, then the setup built afresh, then the submission;
`--wait` waits for the copy, `--dry-run` prints the PanDA spec), and with
`--rerun` makes a new attempt of a task already submitted: a new PanDA task,
`.tryN`, writing in its own namespace, all of its ranges run again and nothing
of the earlier attempt reused (Torre Wenaus, 2026-10-28, for p4495 and p4497,
killed in the attempts of that morning). PCS refuses a submission or rerun to
a whole-node queue that carries an EVGEN setup from before this chain
(`prodtask_submit_request`).

The production shape carries three payload switches, which canary 41481 runs
together: `EPICPROD_SWITCH_ES_HANDOFF_IN_PLACE` (a unit's RECO is written in
its handoff directory), `EPICPROD_SWITCH_ES_UNIT_LOCAL` (no unit makes a PanDA
token request, catalog query or log upload of its own) and
`EPICPROD_SWITCH_ES_INPUT_NAMES` (units and closes named from the job's own
EVGEN file), set in the sandbox's environment file (`production_es.apply`).
From 28 October each unit's handoff, whose one move of a file between
directories on `/pscratch` the first switch removes, was served one at a time
across the node: in job 6289946 the 128 first units passed validation within
9 s of each other and completed their handoffs a median 10 s apart over 21
minutes, and resident units waited 30 to 80 minutes from simulated to done,
0.4 to 3 events a second a node against 27.8 (task 41262). The gun feeder's
shape carries the first two.

The jobs of a task share one event count per file, the smallest of its files'
full counts in whole ranges (`evgen_es.event_service`). A manifest covers each
file in whole jobs of the ordinary shape and leaves its tail of a few thousand
events, so a job reads that tail, which no task covers. p4498 runs run001 and
run002 in task 41494 and the first 996,790 events of run003 in its tail, task
41497: the manifest's 5,680,057 events and 21,834 of the files' uncovered
tails. No event is read twice; the events read are not exactly the
manifest's.

The next step is five nodes on the preempt QOS, which charges half after a
job's first two hours, with six-hour jobs and then eight (Torre's mail to
epic-prod-ops, 26 October); the QOS, the Slurm time limit and the worker count
are in PanDA operations' harvester. It starts with a controlled preemption:
SysConfig `perlmutter_production.trial`, the shape keys `preempt_at_s`,
`preempt_pilot` and `preempt_first_job`, makes the next cycle submit one
sample beside the running work, a second node, and then clears. Its first job
cuts itself at `preempt_at_s` (1,200 s: several closes stood, and some not yet
reported), killing the pilot and everything under it with nothing reported,
as when a node is taken away (NODE_EVENT_DISPATCHER.md, Preemption, The test);
the close-out must credit its closes that stood and release the rest, and the
sample's next job runs whole.

The first trial (task 41316, job 6087270, 26 October) saved 249,480 events in
seven closes, and PanDA counted the 122,012 the pilot had already reported.
The harvester saw the Slurm job end and ended the PanDA job three minutes after
the cut (taskbuffer 300, "The worker was finished while the job was running"),
before the close-out, which acts only on running jobs; the server released the
other 127,468 ranges to the file, though their output stood registered.
`tools/es_credit_ended.py` put them back as finished from the job's record, and
reports at once (The batch pilot) now credit a close as it stands, so a node
lost without notice loses only work not yet closed.

A real preemption gives notice: NERSC's preempt QOS sends SIGTERM to the
srun-launched processes 60 to 120 s before SIGKILL. With
`EPICPROD_SWITCH_ES_NOTICE=1` (`notice` in the production ES shape, on for the
Perlmutter feeder's tasks) the harness ends the job inside that notice: it takes
no more ranges, gives up the units on its slots and those awaiting a close
(their ranges return to the server unreported), gives a running close 30 s to
stand, and takes the pilot's receipt; the pilot reports straight to the server
and sends its final update. The trial key `preempt_notice_s` makes the cut such
a notice (`ES_PREEMPT_NOTICE_S`: SIGTERM to the pilot and everything under it,
SIGKILL that many seconds later), which is the test of this path.

## The end of a job

A worker's allocation lasts until its pilot exits. In task 41256 the ES
harness ended at 04:04:18 ET, PanDA recorded the job's end at 04:09:52, and
the pilot exited at 04:12:15 (launch record of worker 21955): eight minutes
with all 128 cores idle, about 3% of a four-hour allocation. The batch
pilot's final report no longer archives and uploads receipts (The batch
pilot). The pilot's own log stays private, so after the pilot exits the
launcher appends its end-of-job step lines to the public launch record:
timestamped step messages only, with anything credential-like removed.

Two limits end a job without its pilot's final report:

- **The work directory.** The pilot kills the payload's process group, its
  own included, when the job's work directory exceeds `maxwdir`. Through
  payload 0.25.17 each close kept its merged file, so the directory grew by
  about 0.5 MB for every event saved; task 41260 was killed that way 55
  minutes in, at about 34 GB (exit 137 in its launch record). Closes now
  remove the merged file once it stands at its RSE, and the resident EVGEN
  service removes each request's range file. Small units fill it too: each
  unit's files carry a fixed overhead, and closes of hundreds of them fall
  behind. The first production job (6071469, task 41287) ran 5-event units
  while its ranges were starved (The batch pilot, Ranges in bulk) and was
  killed 20 minutes in at 40 GB, with 14,438 events awaiting a close; the
  10,212 events of its two registered closes were credited, and its other
  ranges returned to the task. The harness now guards the directory
  (payload 0.25.25; EPICPROD_PAYLOAD.md, Event Service closes): from 70% of the
  pilot's limit it starts no unit and closes what it holds, so a slow close
  costs node time rather than the job.
- **The record.** The harness's shipped record allows 1,500 writes per job;
  at 128 slots one write per finished unit would reach it after about two
  hours, and the record would then stand still to the end. Writes are now
  at least a minute apart (JOB_REPORTING.md, The Event Service record). The ES
  close-out does not act on a record at its write cap.

## Payload canaries and probes

The test queue runs the launcher, so a payload canary there (site-canary
IMPLEMENTATION.md § Payload canaries: one manifest row of a
standard-configuration task, the production payload, a verdict from its
report) is the launcher's proof and the pool sample's carrier: every run
leaves a sample of the machine as the worker found it, whatever the verdict.
A payload canary on `NERSC_Perlmutter_epic_es` every twelve hours is the
standing reading of the Perlmutter pool until production runs the launcher.

A landing probe on the production queue is a whole-node Slurm allocation
running the site's wrapper: the harvester brings up a worker for it, and the
worker ends a few minutes later (job 2723589: a 256-core node for five
minutes, for a 1.2-minute probe). The probe interval is a property of the
queue on the canary probes page; for `NERSC_Perlmutter_epic` it is 12 hours.

## Commissioning record

| Task | Shape | Result |
|---|---|---|
| 41126 | first native ES smoke, 20 events | all ranges completed; two closes registered; 20 IDs and 387 collections read back |
| 41148, 41157 | 26.07.3 neutron gun, 20 events | the pilot-proxy token handoff, then unique event IDs per range |
| 41159 (`pES1`) | 1,000 gun events | all credited and read back; the input lasted minutes, 7.9% of core time |
| 41203 (`pES2`) | 1,000,000 gun events, 256 slots | the record stopped before five minutes; first units reached the slots over 140 s |
| 41215 | 128 slots, cohort start | first processing within 1.18 s across slots; only the first close saved |
| 41226 (`pES5`) | first batch channel | the adapter used `pandaid` where native `JobData` has `jobid` |
| 41229 (`pES6`) | 128 slots, one EICrecon per slot | 8 GB available after eleven minutes; the record stopped; 418 events credited |
| 41241 | resident EVGEN, 4 slots, 64 events | one npsim per slot across units; all events read back against the source |
| 41249 | 128 slots, shared reconstruction | memory 135-140 GB available; 34,416 events read back; 87.6% simulating |
| 41256 | 128 slots, asynchronous reconstruction | 92.9% simulating; the last two closes went uncredited (the report barrier followed) |
| 41260 | four-hour shape | killed at 55 minutes by the work-directory limit (closes kept merged files) |
| 41262 | four-hour shape | 99.6% steady simulation, 1.8 s turnaround |

Still to establish: a bounded worker-loss and recovery run with the batch
pilot.

## Related

- [NODE_EVENT_DISPATCHER.md](NODE_EVENT_DISPATCHER.md): the design the
  test queue serves; the pilot side and the round trip proved on
  `BNL_NPPS_GPU`.
- [EPICPROD_PAYLOAD.md](EPICPROD_PAYLOAD.md): the payload's ES machinery.
- [GKE_PILOT_FLOW.md](GKE_PILOT_FLOW.md): the Google ES pilot, which shares
  the batch pilot.
- [NPPS0_TEST_QUEUE.md](NPPS0_TEST_QUEUE.md): the proving program, and
  what that queue cannot represent, which is the Perlmutter material
  here.
- [OSG_SUBMISSION.md](OSG_SUBMISSION.md): pilot selection and the
  canary pilot directory, which Perlmutter does not mount.
- site-canary `docs/PLAN.md`: the probes and the fingerprint.
