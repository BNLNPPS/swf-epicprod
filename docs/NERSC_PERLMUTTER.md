# NERSC Perlmutter: how pilots start, and the test queue

Perlmutter runs ePIC production through a harvester operated by PanDA
operations on the site's login node. This document records how a pilot
comes to run on a Perlmutter node, what of that path is reachable from
the production system, and the arrangement under which production
operations controls the pilot on its own Perlmutter test queue. The
Event Service design that the test queue serves is
[NODE_EVENT_DISPATCHER.md](NODE_EVENT_DISPATCHER.md); the proving
program that precedes Perlmutter is
[NPPS0_TEST_QUEUE.md](NPPS0_TEST_QUEUE.md).

## The queues

The site is commissioning for Event Service. The ordinary production queue
remains off; `NERSC_Perlmutter_epic_es` is the commissioning lane and automatic
production feeding stays disabled. The allocation monitor preserves an
explicit commissioning state. Start with short native-pilot smoke jobs and
verify ES execution, output registration and readability before moving to
production-shaped validation.

| Queue | Role | Record |
|---|---|---|
| `NERSC_Perlmutter_epic` | production | `jobseed` `std`; mcore; `maxtime` 14400 s |
| `NERSC_Perlmutter_epic_es` | production operations' test queue for the Event Service | created 2026-09-10 as a clone of `NERSC_Perlmutter_epic_dev`; `jobseed` `all`; mcore; `apptainer:pilot` with `/global`, `/cvmfs` and `/pscratch` bound; rucio copytool to `BNL_PROD_DISK_1`; `maxtime` 345600 s; ordinary jobs run on it |

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

## The worker record is public

`https://portal.nersc.gov/cfs/m3763/panda/jobs/<queue>/<pandaid>/`
serves, without login, the pilot log, the Slurm job's stdout and the
task's stdout and stderr; the task stdout carries the rendered wrapper,
the environment, the container command and the pilot command line
before the pilot's own log. The job record's `pilotid` names the
directory, and `panda_study_job` returns these files as `log_urls`.
Everything in the section above is read from there.

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
`status`) stay in CRIC.

For the Event Service the file must declare the `es_events` and
`es_failover` activities, in `acopytools` as `rucio` and in `astorages`
as `BNL_PROD_DISK_1`, the route the queue's ordinary outputs take.
Undeclared, the pilot's event service stage-out forces the
`objectstore` copytool (`pilot/api/es_data.py`) and fails against a
storage that is not an object store.

The shipped `zip_time_gap` is 300 seconds, matching the established Google
queue setting. The effective value must be checked through the pilot's full
information-service resolution: its configuration provider and the
`zip_time_gap` alias can compete. A bare `QueueData` construction or reading
the provider's literal 601-second default does not establish the running
pilot's value. The Perlmutter batch candidate sets both names and verifies an
effective 300-second interval locally. The earlier 60-second setting was
incorrect; pES4 credit reconciliation did not validate that proposed repair.

## Controlling the pilot on the test queue

Production operations owns the pilot launch that runs on
`NERSC_Perlmutter_epic_es`: the script that prepares what a pilot needs
on the node and starts it. It lives in this repository at
`perlmutter/NERSC_Perlmutter_epic_es/epicprod-perlmutter-pilot-launch.sh`,
named to travel alone (a cached copy in the site's tree, a copy in
every worker directory): whose it is, where it runs, what it does. It
is served from `main`, as everything in this repository is: `main` is what
runs.

The launcher is a site adapter to the **common native pilot**, using the
Perlmutter-only `native-runtime-perlmutter-es-batch-afeaebd` bundle and archive
`98a30c3237d742655faed267bb8f3dbb7ba634c10e08e3b68054e92899075028`.
It adds bounded batch IPC to the previously approved native baseline. It starts the campaign image's own Python through
Apptainer, without ALRB. Event Service uses that pilot's generic executor
and a matching Python 3.13 yampl channel; there is no separate ES pilot fork.
The runtime and channel arrive over immutable, checksum-pinned HTTPS URLs
under the devcloud bucket's public `pilot/` prefix because the EIC CVMFS
repository is unavailable on these nodes. A failed artifact check ends the
launch before dispatch; it never selects the legacy pilot as a fallback.

Image admission reads the same fixed public launcher URL through the cached
product `perlmutter_pilot_runtime` (five-minute refresh, fifteen-minute maximum
evidence age). It derives the exact image and pinned runtime hashes from the
adapter. Missing, unreadable, ambiguous or stale evidence refuses submission.
This establishes the launcher's capability; it does not assert that a worker
has started or an ES payload has succeeded.

The existing Slurm script can still start eighty copies of the adapter.
Only rank zero continues, producing one pilot on the single-node allocation;
all other ranks exit before staging anything. The pilot advertises 128 physical payload cores, retains the Harvester identity and uses a mode-0600 copy
of the site's proxy. `/global`, `/pscratch` and `/cvmfs` remain accessible.
Local queue and storage configuration retain the `es_events` and
`es_failover` routes. Pilot console output stays private; the public launch
record contains adapter messages and selected startup markers, without an
environment dump.

The adapter resolves the scheduler's uid/gid on the host and stages private
passwd/group lookup files before entering a user and mount namespace. Those
files are bound over the lookup paths only in that process namespace, so the
CVMFS Apptainer can resolve the account even without the site's NSS service.
The host account files and numeric identity remain unchanged. A failed host
lookup stops the launch before dispatch.

Commissioning workers use a forty-five-minute ceiling
including staging, clamped to an earlier Slurm end time. HEPiX `shutdowntime` exposes the same
deadline to the common pilot; its first-job admission minimum is ten minutes.
A worker takes at most one job and then exits. The initial ES smoke shape is
20 events, four explicit execution slots and 1,500 MB/core, so whole-node
memory accounting remains below the node's capacity. The site's existing
Slurm template is unchanged; the adapter bounds actual worker lifetime.
The original twenty-minute ceiling was for startup and output-path smoke tests.
The later four-hour ceiling was replaced to bound the requested roughly
thirty-minute production demonstration, including staging and final handling.

The first native ES smoke passed on 25 October at 00:40:56 UTC:
task 41126, job 5904471, worker 21934 (Slurm 60232318), using
`26.07.1-stable`. All 20 ranges completed without failed units; two RECO closes
registered 5 and 15 events. Independent JLab Rucio reads confirmed both files,
and the campaign's ordinary PODIO reader read all 20 distinct event identities
and all 387 collections. This establishes the short native ES path on
Perlmutter; it does not establish 26.07.3 gun execution or worker-loss recovery.

Native ES canaries use `submit-evgen-task.py --es-worker-proxy`: the sandbox
names a local proxy path, and the dispatcher copies the native pilot's existing
credential into that path on the worker with mode 0600. The live credential is
not uploaded in the task sandbox. The normal canary output namespace and
lifetime are unchanged.

Both `X509_USER_PROXY` and `PILOT_X509_USER_PROXY` must name that worker-local
file in the sandbox environment. ES closes start under `--cleanenv` and source
the environment afresh. The second name lets the registrar exchange the pilot
identity for PanDA-issued Rucio and storage write tokens. Catalog authentication
with a proxy does not establish write permission at the output door.

The first 26.07.3 gun ES checks (Google 41140 and Perlmutter 41141) exposed this
handoff requirement: reconstruction and merged-file validation passed, but the
close lacked `PILOT_X509_USER_PROXY`, fell back to X.509 and was denied by the
output door. A controlled reproduction with a registered neutron file failed
in the same way; retaining the pilot reference obtained the tokens, transferred
and checksum-verified the file, and registered it. Actual worker reruns remain
the acceptance gate for the correction.

The adapter also samples `squeue` and `sinfo` before entering the container,
writing `pool-sample.json` above its work directory (`pool-sample/1`). It
exports that path as `EPICPROD_POOL_SAMPLE` and its Apptainer equivalent so
the payload report carries the account's and machine's running/pending jobs,
nodes and partition state (swf-monitor `docs/POOL_REPORTER.md`). The existing
node-exclusion check remains before pilot startup.

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
the site's file name.

The scope is set by the URL: a queue has a cached copy only if a file
is published under its name, so only `NERSC_Perlmutter_epic_es` runs
production operations' wrapper; every other queue's fetch fails and
the site's wrapper runs as before. On the test queue a successful fetch
replaces the cached copy atomically; a failed fetch leaves it, and the
worker runs the last good version; the site's wrapper is reached only
before the first successful fetch, and a one-time seed of the cached
copy removes even that. Once a queue has a cached copy, removing the
file from the repository does not return the queue to the site's
wrapper; every change in either direction is a publication on `main`.

The request to PanDA operations is these four lines in the Slurm job
script, the seed, and that the Perlmutter harvester configuration,
Slurm template and wrapper be kept in a repository, so that a change
like this one is a pull request. PanDA operations changed the
`NERSC_Perlmutter_epic_es` Slurm script on 2026-09-13 to fetch the
launcher from its fixed URL (not the `${PQ}` form, so the production
queue keeps the site's wrapper); the first worker to run it is the
payload canary of the section below.

## Payload canaries on the test queue

The production queue's landing probe (below) runs the site's wrapper
and says what production sees. The test queue runs the launcher, so a
payload canary there (site-canary IMPLEMENTATION.md § Payload canaries:
one manifest row of a standard-configuration task, the production
payload, a verdict from its report) is the launcher's proof and the
pool sample's carrier: every run leaves a sample of the machine as
the worker found it, whatever the verdict. A payload canary on
`NERSC_Perlmutter_epic_es` every twelve hours is the standing reading
of the Perlmutter pool until production runs the launcher.

## Canary probes at the site

A landing probe on Perlmutter is a whole-node Slurm allocation: the
harvester brings up a worker for it, and the worker ends a few minutes
later (job 2723589: a 256-core node for five minutes, for a
1.2-minute probe). The probe interval is a property of the queue on the
canary probes page; for `NERSC_Perlmutter_epic` it is 12 hours
(2026-09-12). Between probes the queue's health reading comes from the
production jobs on it, which a fed queue always has.

### Native 26.07.3 gun registration proof (25 October UTC)

Task 41148 / job 5910732 finished all 20 events at 01:49:06Z using the
native worker and the corrected pilot-proxy token handoff. The JLab catalog
independently lists one registered 20-event close. The normal 26.07.3 PODIO
reader read all 387 collections and found 20 distinct generated-truth
momenta. That run predates the ES gun event-number correction: its headers
contain only five distinct IDs, so it is not the acceptance proof for new
production event identity.

Task 41157 / job 5911833 subsequently proved the event-number correction on
Perlmutter: all 20 events finished and registered, with 20 unique event IDs,
20 distinct generated-truth records and all 387 collections readable. The
next operator-authorized whole production sample is task 41159 (`pES1`):
1,000 additional neutron-gun events in one job, 50-event units, a 20-minute
deadline and five-minute drain margin. Its sample stem separates its random
streams from both the source production and Google's `gES1` sample. It uses
the existing Harvester/Slurm path and normal RECO retention; the site remains
commissioning and automatic feeding remains disabled.

Task 41159 / job 5912564 registered and credited all 1,000 events in two
production RECO files. Independent 26.07.3 readback verified event IDs 0–999
exactly once, 1,000 distinct neutron truth records at 3–50 degrees, and all
387 collections readable. The sample's output checks are recorded in its PCS
metadata; this is separate from the physics-validation service's verdict.

The 1,000-event sample exhausted its input in minutes and occupied only 7.9%
of its logical core-time. It proves the output path, not sustained ES slot use.
Task 41203 (`pES2`) supplies 1,000,000 fresh neutron-gun events to two initial
long consumers, rolling one-minute closes and enough work to stay busy through
the allocation. Workers have a four-hour ceiling; the ES harness has a
3-hour-45-minute deadline including its five-minute drain margin, reserving
time for staging and final pilot output handling. Deferred ranges remain in
the task's ES ledger for subsequent consumers. Acceptance requires the observed per-core timeline, repeated
registered closes during processing, and complete independently readable event
coverage. A declared deadline or successful tiny task is not that evidence.

The first `pES2` record stops before five minutes and contains only two registered
events. A terminal PanDA status without a final harness record does not establish
that its then-active units were lost, nor that the observation spans the full
run. Its initial units reached the 256 slots over about 140 seconds, so it also
does not establish a parallel whole-node start.

The opt-in `parallel_start` production-ES setting exports
`EPICPROD_SWITCH_ES_COHORT_START=1`. Containers initialize while the harness
collects the first cohort's ranges, then all healthy slots receive their first
units together. The unset switch preserves existing tasks' behavior. Fresh
commissioning samples use 300-second closes, a 2,100-second harness deadline
with a 180-second drain margin, and the forty-five-minute worker ceiling.
The CPU-node shape uses 128 execution slots, one per physical core; the 256
logical CPUs include two hardware threads per core. This keeps the simultaneous
simulation and resident-reconstruction processes within the node's memory
budget. See [NERSC affinity](https://docs.nersc.gov/jobs/affinity/).

## Related

- [NODE_EVENT_DISPATCHER.md](NODE_EVENT_DISPATCHER.md): the design the
  test queue serves; the pilot side and the round trip proved on
  `BNL_NPPS_GPU`.
- [NPPS0_TEST_QUEUE.md](NPPS0_TEST_QUEUE.md): the proving program, and
  what that queue cannot represent, which is the Perlmutter material
  here.
- [OSG_SUBMISSION.md](OSG_SUBMISSION.md): pilot selection and the
  canary pilot directory, which Perlmutter does not mount.
- site-canary `docs/PLAN.md`: the probes and the fingerprint.

## Bounded ES batch-channel correction

The pES4 per-core record failed sustained-occupancy acceptance: higher-numbered
slots waited over six minutes, and only eight of 128 slots were processing at
minute 14. Its registered output/readback and later accounting reconciliation
do not constitute Perlmutter preemption validation.

The new Perlmutter-only candidate gates `EPICPROD_SWITCH_ES_BATCH_CHANNEL=1`.
It takes up to 256 ranges from the pilot's existing cache per IPC request and
reports one completed unit with all of its range IDs. The pilot uses its normal
bulk event API, with at most 10,000 IDs per report request. It packs each unit
record once through a single tar writer, avoiding a shell tar invocation and a
repeated record for every event. Range acquisitions are bulk requests at least
two seconds apart. The explicit pilot report interval is 300 seconds; both
configuration field names are set so the full information-service resolution
cannot select a conflicting queue alias.

The harness refills fragmented buffers in bounded batches, retains a hard
buffer limit, and feeds the longest-idle available slot first. Existing Google
and GREX runtimes retain their installed archives. The candidate is built from
exact approved native archive `9532f30b` by `scripts/build-native-es-batch.py`.
Local checks exercise actual candidate pilot classes over real yampl in the
campaign image, including full information-service configuration, 128 simulated
slots, exact owned event IDs, bounded API batches and one stored record per unit.
A new bounded Perlmutter worker-loss and recovery run is still required before
calling this correction commissioned.
