# ePIC Production on the OSG Pool

How ePIC production jobs reach the Open Science Grid (OSG): the hosts,
the harvester and HTCondor configuration that submits the pilots, the
pool they run in, the container layering that results, and the levers
that are under production operations control. Facts below were read
from the live hosts and the pool on 2026-09-01; the dated snapshots
should be refreshed when the configuration changes.

## 23 October 2026: direct pilot in OSG production

At **14:38:35 UTC (10:38:35 ET)**, on Torre's production Go after the
20-pilot trial, `BNL_OSG_PanDA_1` switched new Harvester submissions to
the direct pilot in `eic_xl:26.07.1-stable`. This section supersedes the
older ALRB launch description below for that queue. Existing pilots finish
normally; other queues and their templates were not changed.

The existing live template path remains
`/var/data/atlpan/harvester_common/submit_pilot2_pull_bnl_osg.sdf` on
osgsub01. Its executable now explicitly names
`/var/data/atlpan/harvester_common/native-production-20261023/epic-native-pilot.sh`,
overriding the old `executableFile` value still present in queue configuration.
Harvester reads the template for each submission; no restart was required.
`scripts/render-native-production.py` derives the replacement from the old
template and preserves Harvester IDs, worker reporting, log paths, proxy,
resource requests, priority, site exclusions and the dynamic node guard.
Wrapper-only arguments are replaced by pilot.py arguments and explicit
queue/storage data URLs. The standalone trial's
`--noworkerpilotstatusupdate` is deliberately absent.

The slot must advertise `HasSingularity` and the image CVMFS repository.
The ATLAS repository, host RHEL 9 and unprivileged-user-namespace requirements
are removed. `+SingularityImage` selects the image; nesting is disabled.
The archive, CA bundle, Rucio configuration, deadline helper and username
helper travel with the pilot. The frozen asset directory above contains
archive SHA-256
`74b323e14ca5b3e35e79711e6fbcfd1c8fca3c0749fc7a29b5d8e57d49076060`
and launcher SHA-256
`447af86c75c7b12736682816f93f75d2266b1a1408d27cce1d6f08a6d23b3763`.
The launcher includes the `USER`/`LOGNAME` fallback and glidein-deadline
handling. These additions have less field exposure than the original cohort.

The ramp is normal Harvester refill: at most 600 waiting pilots and 300
new workers per cycle, subject to demand. No worker-limit increase or batch
removal accompanied the switch. Condor dry-run rendering succeeded and the
20 native tests passed. First real Harvester submission: `27496.0`, worker
`2364932`, at 14:38:50 UTC, with the expected image, direct executable,
transferred assets and `harvester-BNL_osg_harvester_1` scheduler identity.
PanDA independently confirmed task 41040 running in `27498.2` (job
5846574, 14:40:50 UTC), `27498.1` (5846575, 14:40:52 UTC, UA–Little
Rock) and `27498.0` (5846586, 14:41:08 UTC, SU-ITS), with the normal
Harvester scheduler identity and requested image. At 14:40 the new cohort
already had 15 submitted pilots, six running and nine waiting, with no holds.
This verifies submission, launch and payload acquisition; completion and
registration of these production jobs have not yet been observed.

This deployment serves a **single image**. Current active queue jobs were
checked against 26.07.1. A different-image job would be refused after
acquisition, before payload execution; this is not an attempt-free dispatch
filter. Image-aware placement/dispatch must precede adding another image to
this queue. Existing node-guard attribution of synchronized worker losses
remains a separate issue; the rollout does not disable the guard.

Rollback template, preserved beside the live file:
`submit_pilot2_pull_bnl_osg.sdf.before-native-20261023T143835Z`.
Restoring it changes future submissions only; refresh its dynamic node-guard
line through the existing guard updater. Keep the frozen native assets while
any submitted direct workers still reference them.

## Execution-queue admission headroom

`pilot/native/queue-runtime-policy.json` carries the minimum time a new
job needs, separately for each PanDA queue and exact execution queue.
The execution key is `GLIDEIN_Site` from this execution's machine ad,
which PanDA records as `destinationsite`; it is not inferred from a
hostname or an institutional abbreviation.

| PanDA queue | Execution queue | Minimum remaining time |
|---|---|---|
| BNL_OSG_PanDA_1 | SU-ITS | 18,900 s (5 h 15 min) |
| BNL_OSG_PanDA_1 | OSG_US_FSU_HNPGRID | 24,300 s (6 h 45 min), initial floor |
| Other combinations | Default | 9,000 s (2 h 30 min) |

The SU-ITS value uses task 41051's admitted Haswell p99 of 281–285
minutes plus the existing 1,800-second allowance for shutdown grace,
delivery and setup, rounded up to fifteen minutes. The x86_64-v2
exclusion remains in force. FSU has a censored sample: finishes reached
342 minutes and old-limit failures reached 364 minutes. Its initial
floor uses that observed lower bound plus the same allowance, rounded
up; it is **not** a measured uncensored p99. Review FSU after a day of
completions under the ten-hour limit. The comphy host's fast EPYC CPU
does not establish why its jobs ran slowly.

A positive explicit `EPICPROD_MIN_TIME_FOR_NEW_JOB` wins over the file.
Otherwise precedence is execution queue, PanDA queue, then default.
The reader logs the exact queue keys, selected seconds and source,
exports the value before the first-job deadline decision, and the
existing archive reads that same variable before every subsequent job.
The pilot compares the smallest remaining budget: its own clock from
pilot start, proxy validity, and MachineFeatures shutdowntime. This
policy neither extends those clocks nor resizes any submitted manifest.
A damaged shipped policy stops startup before work is acquired.

The renderer derives the matchmaking headroom requirement from this same
policy: `TARGET.GLIDEIN_ToDie - time()` must exceed the execution queue's
minimum (or the default for an unknown queue). Slots with no advertised
death time retain their existing treatment. The launcher's landing check
remains the backstop. This avoids repeatedly matching pilots to a dying
glidein only to decline it after startup. Updating an existing template
also replaces the earlier uniform two-hour gate; unfamiliar lifetime
expressions require review rather than silently adding another rule.

On 24 October the same policy also sets the pilot maximum: 57,600 seconds
at `OSG_US_FSU_HNPGRID`, 36,000 at other OSG execution queues, including
an unknown execution queue. The launcher copies the current server queue
record, changes only `maxtime`, and gives the pilot that local file. This
pins both the pilot monitor and its repeated-job admission clock even when
the shared PanDA queue advertises the higher ceiling. The existing admission
minimums, including Fir's live 7,800 seconds, are retained.

The submit template records `GLIDEIN_Site` into `MachineAttrGLIDEIN_Site0`
at match time. Its `MaxRuntime` is 960 minutes for FSU and 600 otherwise;
`periodic_remove` enforces that duration on the running worker, alongside
the existing safeguard. The renderer derives these values from the same
policy as the launcher. Existing workers retain their transferred assets.

The policy and `runtime-policy.py` travel with each pilot as frozen
input files. Both the production renderer and cohort submitter include
them. `render-native-production.py --retarget` changes an existing
native template's asset paths and deployment tag, retaining its image,
Harvester identity, resource requests, runtime, CPU restrictions and
node guard. A cohort must explicitly resolve a Harvester runtime
placeholder with `--runtime-minutes`; it must not silently reuse the
former six-hour value. Existing workers keep their transferred assets.

## 23 October: image-aware dispatch verification

The server on pandaserver01 gained the optional `container_name` filter
at 15:34:49 UTC (`a754853d`, on its deployed `725129f3` base). The native
builder now adds that field to `get_dispatcher_dictionary` only when
`PILOT_CONTAINER_NAME` is nonempty. The launcher sets it after checking the
actual outer image, using the exact configured CVMFS spelling rather than
its resolved path. There is no retry without the image constraint.

Candidate assets are frozen on osgsub01 in
`/var/data/atlpan/harvester_common/native-image-dispatch-20261023`, archive
SHA-256 `092ac7052bddb9dc644c4c0fc53d9a6b305ab6b9661188bb1411c5176ceee384`.
At 15:50 UTC, four matching probes (`27597`–`27600`) and two negative
probes (`27601`–`27602`) were submitted; a fifth matching probe (`27605`)
followed. Five PCS trials, tasks `41072`–`41076`, each request ten events
and their own FULL/RECO/log outputs. The matching pilots use 26.07.1;
the negative pilots use the real 26.07.3 image, which cannot match these
tasks. Task binding keeps every probe away from long production jobs;
the negative probes also bind a trial task, testing that task and image
constraints intersect. Logs are under `condor_logs/native-image-dispatch-20261023`.
Two initial trials were acquired by ordinary production pilots and are
controls, not declaring-pilot evidence. Replacement ten-event trials
41077 and 41078 used the test work queue and declaring pilots 27617.0 and
27618.0. Three declaring jobs finished with registered physics outputs,
ready logs and zero payload, pilot or DDM errors:

| Trial | Job | Pilot | Site | Log bytes |
|---|---|---|---|---|
| 41074 | 5856208 | 27599.0 | SU-ITS | 144811 |
| 41077 | 5856309 | 27617.0 | ComputeCanada-Fir | 145636 |
| 41078 | 5856308 | 27618.0 | ComputeCanada-Fir | 141600 |

The decisive negative probes were 27620.0 and 27621.0 on SU-ITS: neither
bound a task, both declared the full 26.07.3 path, and both received
“No jobs in PanDA” twice before exiting zero. At that time 3,196 activated
jobs all carried 26.07.1. Their stdout records the request and response;
the server's INFO log does not print the DEBUG parameter line. A negative
answer from an empty bound task was not counted as image-filter proof.

On Torre's go, the production template switched at **16:41:50 UTC** to
these frozen assets and tag `production-image-dispatch-20261023`. Only
the executable, input asset paths and tag changed; Harvester identity,
worker reporting, image, node guard and ramp settings were preserved.
The backup is `submit_pilot2_pull_bnl_osg.sdf.before-image-dispatch-20261023T164150Z`.
The first declaring production cluster was **27675**, submitted at
16:41:54 UTC; worker 2366068 is 27675.0. The three-hour observation window
before the upstream PanDA PR ends at 19:41:50 UTC.

Deployment order: server first, then declaring pilots. Before introducing
another image to production, all old direct pilots that acquire without a
container constraint must finish. For a server rollback, stop or drain the
clients that send the new field first: changing future submissions alone
does not change requests from running pilots. An old server rejects the new
argument rather than silently ignoring it.

## Image allocation within the shared waiting budget

Harvester remains the sole pilot refiller, with one combined cap of 600
waiting workers. `pilot/native/image-policy.json` declares the approved
images. `image-allocation.py` reviews grouped runnable demand and current
Condor workers every two minutes; it accounts for the image distribution
already running and rebalances only idle workers. The image environment,
outer container and declared dispatch image change in one queue transaction.
Running workers retain their image and finish normally.

Waiting targets never exceed runnable job counts or the shared budget.
Existing assignments are retained where possible, with the oldest excess
workers available to a newly needed image. A runnable image gets at least
one opportunity, preventing small canaries from waiting behind production.
Protected trial workers are counted but never retagged. Surplus idle
workers have `EpicprodImageReady=False`; the template's Requirements keeps
them unmatched until work exists. This does not create held workers or a
second submission stream. Fresh Harvester refills wait for the allocator's
first decision before matching.

After a scheduler restart, formerly running workers can return to idle and
temporarily exceed the waiting limit. The allocator still enables at most
600 waiting workers, parks the excess with the same readiness gate, and
continues publishing fresh state. It does not remove those workers or
increase the shared budget.

The demand endpoint is one active-job aggregate, with the native SCORE
profile's memory and core limits. The pilot still checks actual free disk
space: its job requirement cannot be compared to Condor's minimum disk
reservation as though those were the same measurement. The allocator reads
the template's resource requests and records query and fetch timing.
Missing or stale demand refuses edits. Its active state expires after six
minutes; the host reporter advertises both images only with fresh matching
state, and the existing image admission check then enforces that evidence.

`scripts/configure-osg-images.py` installs the controller and policy, adds
the template readiness gate, refreshes idle Requirements and installs the
two-minute cron with backups. Installation follows a real matching-image
test job through completion and output registration. The shared epic5
archive replacement retains its separate GREX native-canary gate.

## Hosts and roles

Two harvester instances submit ePIC pilots, on two SCDF hosts.

| Host | Harvester id | Queues served | Submits to |
|---|---|---|---|
| `pandaharvester01.sdcc.bnl.gov` | `BNL_harvester_1` | BNL_EPIC_PROD_1, BNL_PanDA_1, E1_BNL, E1_JLAB, UM_GREX_PanDA_1, BNL_OSG_2 | HTCondor-CEs named in CRIC per queue (BNL spoolce01/02, the Manitoba GREX CE) |
| `osgsub01.sdcc.bnl.gov` | `BNL_osg_harvester_1` | BNL_OSG_EPIC_PROD_1, BNL_OSG_PanDA_1, BNL_OSG_PanDA_CI, BNL_OSG_PanDA_pilotest, BNL_OSG_PanDA_test | the OSG pool directly, through a local HTCondor schedd |

Both run harvester as user `atlpan` under `/opt/harvester`, with the
live queue configuration at `/opt/harvester/etc/panda/panda_queueconfig.json`
and the submit templates, pilot wrapper, and proxy under
`/var/data/atlpan/harvester_common/` (osgsub01) or
`/data/atlpan/harvester_common/` (pandaharvester01). No glideinWMS
component runs on either host.

Production operations (`wenauseic`) has passwordless root on both
since 2026-09-01, alongside pandaserver01. The PanDA service
maintainer is told in advance of any production-impacting change;
every edit leaves a dated backup beside the file and is recorded here.

## The OSG queue's submission path

`BNL_OSG_EPIC_PROD_1` is defined in CRIC with no compute element,
`catchall` `osgpool=true`, pilot manager Harvester, workflow `push`.
On osgsub01 it inherits the `production.push` template queue
(HTCondorSubmitter, one worker per job, `truePilot`, CRIC-driven) with
its own submit description
`submit_pilot2_push_bnl_osg.sdf` and the BNL pilot wrapper
`bnlpanda.runpilot2-wrapper.sh` (version 20250605a-eic) as the
executable; worker ceiling 10000, 2000 new workers per cycle.

The wrapper fetches pilot3 from CVMFS
(`/cvmfs/eic.opensciencegrid.org/panda/pilot/pilot3.tar.gz`) and runs
it with `-e eic --pilot-user epic --harvester-submit-mode PUSH` against
pandaserver01, BNL Rucio for traces off. Condor logs are served at
`https://osgsub01.sdcc.bnl.gov/condor_logs/`.

The schedd's primary collectors are `jlab-cm.osg.chtc.io` and
`scicollector901.jlab.org`: these describe the JLab glideinWMS pool with
an OSG-hosted central manager. A live configuration check on 22 October
2026 also found `FLOCK_TO`, `FLOCK_COLLECTOR_HOSTS` and
`FLOCK_NEGOTIATOR_HOSTS` pointing at `cm-1.ospool.osg-htc.org` and
`cm-2.ospool.osg-htc.org`. The submit host therefore also acquires OSPool
slots. A default `condor_status` or matchmaking analysis covers only the
primary pool; use `-pool cm-1.ospool.osg-htc.org` to inspect OSPool as well.
Do not infer a site's disappearance from its absence in the JLab listing. The slot classads name what provisions
it: the OSG production factories (`GLIDEIN_Factory` NRP-Prod and
Tiger-Prod) serving the JLab VO frontend (`GLIDECLIENT_Name`
`JLabVO-1_0.clas12` and `JLabVO-1_0.gluex`), through entries created
for the CLAS12 and GlueX experiments (`CLAS12_T3_UK_ScotGrid_GLA`,
`CLAS12_T1_IT_CNAF_*`, `CMSHTPC_T2_US_MIT`, `OSG_US_UConn_gluskap`,
`Glow_US_Syracuse`, `OSG_US_JLAB_ce-2`, `CMSHTPC_T2_US_UCSD`,
`CMSHTPC_T2_FR_GRIF_LLR`). Those JLab ads have no EIC frontend group or EIC entry: ePIC pilots
run as passengers in the pool the JLab VO frontend requests for CLAS12
and GlueX. This describes the primary pool, not the additional OSPool
capacity reached through flocking. No glideinWMS component runs on the BNL hosts; the frontend is
the JLab VO's, hosted with OSG, and its configuration is reachable only
through JLab's OSG contacts.

### Pull mode on the OSG queues

Since 2026-10-19 `BNL_OSG_PanDA_1` and `BNL_OSG_EPIC_PROD_1` run in pull mode. Its pilots start without
a job and ask the PanDA server for one when they begin, so the job dispatched
is the highest-priority job activated at that moment. In push mode harvester
binds each job to a pilot when it submits the pilot, often hours before the
pilot starts, and later priorities cannot reach the jobs already bound.

On osgsub01 both queues use the template queue `production.pull` in
`panda_queueconfig.json`: the `production.push` settings with `mapType`
`NoJob` and no job-fetch limits, so the job fetcher skips them. Their submit
descriptions, `submit_pilot2_pull_bnl_osg.sdf` and
`submit_pilot2_pull_bnl_osg_epicprod.sdf` (the latter keeping the canary pilot
URL), are the push descriptions with `--harvester-submit-mode PULL`, without
the job input transfer, with fixed requests `request_memory = 4096` (since 22 October; formerly 3700) and
`request_disk = 4500000` (KiB), and with `priority = 1`, so that within the
shared submitting account pull pilots are matched ahead of any remaining push
pilots. A push
pilot's requests are computed from its job; a pull pilot has no job, and the
template's `{requestDisk}` then takes the queue's 30 GB work-directory maximum,
which few slots offer. Harvester holds the number of waiting pilots at the
smaller of the queue's activated jobs and its waiting-pilot limit (1000), so
pilots are not submitted ahead of work. Backups beside the files:
`panda_queueconfig.json-backup-2026-10-19` (both queues push),
`panda_queueconfig.json-backup-2026-10-19b` (`BNL_OSG_EPIC_PROD_1` still push),
`submit_pilot2_pull_bnl_osg.sdf-backup-2026-10-19` and
`submit_pilot2_pull_bnl_osg.sdf-backup-2026-10-19b`. The first pull pilot
took the queue's highest-priority activated job.

Since 2026-10-21 each pilot takes jobs for 120 minutes (`timefloor` 120 in
the queue records, SCHEDCONFIG_OVERRIDES.md), and harvester keeps waiting
pilots at 30% of running, at least 200 and at most 300 per queue, with at
most 100 new per cycle (`nQueueLimitWorkerRatio` 30, `nQueueLimitWorkerMin`
200, `nQueueLimitWorkerMax` 300, `maxNewWorkersPerCycle` 100; backup
`panda_queueconfig.json-backup-2026-10-21`). Before, with one job per pilot,
the limits were 500% of running, at least 1,000 and at most 10,000 per
queue, 2,000 new per cycle: on 2026-10-21 8,275 pilots waited for about
1,000 running slots, the pool started them oldest first, and
`BNL_OSG_EPIC_PROD_1` sat at 0 running with 8,998 jobs queued behind 3,729
older `BNL_OSG_PanDA_1` pilots. The waiting pilots beyond the oldest 300
of each queue (2,189 and 5,486) were removed that day.

The 22 October regulation correction sets both queues to 600 waiting
pilots, limited by activated demand, with 300 new workers per cycle,
20-second submission lookup, 30-second worker checks and Condor cache,
and 30-second job statistics. Static CRIC-derived records retain their
10-minute Harvester refresh. The earlier 200/300 limit combined with a
30-minute monitor left the pool empty while Harvester still counted
started pilots as waiting. Historical worker configuration IDs also kept
old check intervals alive; the installed operational ceiling applies to
those workers. See [the audit and implementation record](REGULATION_AUDIT_2026-10-22.md).

`HTCondorMonitor` accepts `useCondorHistoryMaxAge` from the queue's monitor
configuration and passes it to the history query. The previous statement
that this was not configurable was wrong. Both queues now use 86400 seconds;
a global compatibility floor applies the same eligibility to historical
worker configurations. A two-hour eligibility window was shorter than a
120-minute timefloor plus the final job, so completed pilots disappeared
from the query and were eventually misclassified as cancelled.

The memory request is 4096 MB on both queues, matching the job limit
advertised to the front and server. The old request was 3700 MB while the
front admitted 4096 MB. This truthfulness has a matching cost: on 22 October,
the pool's raw advertisements included 1542 slots at 3700 MB/core and 1355
at 4096 MB/core, before policy and availability filters. The change excludes
187 smaller slots in that sample; it does not promise those 1355 slots are
available. Existing submitted pilots retain their old request and drain
naturally. Tasks asking 5400 MB remain ineligible for these single-core
pilots. GREX separately allocates eight cores and 48000 MB to eight pilots,
with each job admitted against one core and 6000 MB.


New production goes to `BNL_OSG_PanDA_1`. `BNL_OSG_EPIC_PROD_1` is
closed to new tasks and drains existing tasks in place, by Torre Wenaus's
instruction on 22 October. Its admission closure, disabled probes and
`brokeroff` record are separate from its pilot supply: existing tasks keep
generating and running, and Harvester stops replenishing when activated
demand reaches zero. Both queues remain in the shared OSG accounting
while the drain runs. See [PROD_1_DRAIN.md](PROD_1_DRAIN.md).

### Pull mode, time floor and slot length on UM_GREX_PanDA_1

Since 2026-10-19 `UM_GREX_PanDA_1` also runs pull-mode pilots, from
pandaharvester01: a `production.pull` template queue there (the
`production.push` settings with `mapType` `NoJob` and no job-fetch limits) and
`submit_pilot3_pull_umgrex.sdf`, the push description with
`--harvester-submit-mode PULL`, without the job input transfer, and keeping
the canary pilot URL. Backups: `panda_queueconfig.json-backup-2026-10-19`
and `submit_pilot3_pull_umgrex.sdf-backup-2026-10-19`.

A pull pilot's slot is sized by the submit description, not from a job. On
its own, harvester sizes it from the queue and the PanDA resource type: SCORE
(one core, no memory limit) gives one core and 6,000 MB, the 192 GB of a
32-core GREX node per core, and MCORE (two or more cores, no maximum) would
take the whole node.

Since 2026-10-20 each GREX worker is an 8-pilot slot:
`pilot/epicprod-multipilot.sh` is the executable, and the description asks for
8 cores, 48,000 MB and initially 300 minutes (`+xcount`, `+maxMemory`, `+maxWallTime`).
The launcher runs N standard pilot wrappers (default 8) in one allocation of N
cores and N times the per-core memory, each in its own directory, so their
single-core jobs share the allocation's memory. The wrappers start 15 s apart,
since pilots asking the server for a job in the same instant contend for the
same job rows and the losers get none. Harvester counts the slot as one
worker, so waiting workers are held at 500% of running, at least 125 and at
most 625: the 1,000 and 5,000 single-core pilots of the first pull week,
divided by 8. Backups: `submit_pilot3_pull_umgrex.sdf-backup-2026-10-19b`
(single pilots) and `panda_queueconfig.json-backup-2026-10-19c` (limits
before the division).

The initial policy shipped a `queuedata.json` written by `scripts/pilot-queuedata.py`
(kept at `harvester_common/queuedata/UM_GREX_PanDA_1/`) with `timefloor` 180
minutes and `maxtime` 18,000 s, and the launcher copies it to every wrapper.
The file keeps the server's layout, one field per line: the wrapper reads
fields with `grep`, and a one-line file fails its `container_options` check
and stops every pilot. Each pilot starts new jobs for 180 minutes, then
finishes the one it is running and exits; the time floor never stops a running
job, and the pilot starts no job with less than 30 minutes of `maxtime` left.
GREX jobs ran a median of 14 minutes and at most 80 in the week to 2026-10-19,
so the last job ends well inside the slot. The pilots' jobs fall out of step
after the first, which spreads both their memory peaks and the end of the
slot, and Slurm releases the slot when the last pilot exits.

Since 2026-10-21 each slot draws its own time floor, the shipped 180
minutes plus or minus 30 (`EPICPROD_TIMEFLOOR_SPREAD`), the same for its
eight wrappers. With one floor for every slot, the slots started together
on 2026-10-20 drained and were replaced together: about 100 slots turned
over at once every four hours, and GREX ran about 600 jobs instead of 930
for an hour each time. Each rotation draws again, so the slots fall
further out of step. The latest end, 210 minutes plus the longest job,
stays inside the 300-minute slot. The script is
rerun when the queue's CRIC record changes.

On 22 October the current job mix was measured at a 68-minute median,
roughly 120-minute p99 and 164-minute maximum, so the preceding 14/80-minute
sizing was no longer suitable. The policy for newly submitted allocations
is now 720 minutes, with `maxtime` 43200 and `timefloor` 510 in the GitHub
overrides. JLab explicitly retains its separate 180-minute floor.

The `deadline-v2` multi-pilot launcher draws the intake window around 510
minutes with a 30-minute spread, capped to leave at least 180 minutes for
the last job and five minutes for wrapper setup. Effective intake is about
480–535 minutes. The submit template passes the actual allocation budget;
each child gets time remaining to one common deadline, including the time
spent staggering starts. A newer queue record cannot extend an older batch
budget. Missing or invalid budget data stops the launcher before payloads
start. Logs identify the launcher and policy versions, selected floor and
absolute deadlines.

`scripts/configure-grex-lifetime.py --apply` installs the launcher and GREX
submit-template changes with backups; the launcher is installed first,
before publishing the longer GitHub overrides. Harvester reads the template
for each submission, so this needs no daemon restart. Existing submitted
workers carry their original transferred executable and queue data and
retain their original lifetime; installing a file does not update that cohort.
No existing workers are removed to force turnover.

The longer intake is intended to run roughly seven or eight current jobs
per pilot and amortize the final partially occupied tail. The tail remains;
the recent observed maximum is not a guarantee about future job duration.
CE acceptance, start latency and full-allocation utilization must be measured
for the new cohort before claiming a performance improvement. The decline
alarm latches its reference peak until the incident clears, so expiration
of an old sample alone cannot announce recovery. Evidence and operational
limits are in [the implementation report](REGULATION_AUDIT_2026-10-22.md).

## The submit description

The attributes of `submit_pilot2_push_bnl_osg.sdf` that shape the
pilots (2026-09-01):

```
+ProjectName = "EIC"
+JobDurationCategory = "Medium"
request_cpus = {nCoreTotal}; request_memory = {requestRam}; request_disk = {requestDisk}
Requirements = (HAS_CVMFS_atlas_cern_ch == True) && (OSGVO_OS_STRING == "RHEL 9")
               && (HAS_UNPRIVILEGED_USER_NAMESPACES =?= "enabled")
               && !( (GLIDEIN_Site =?= "Nebraska"
                      && regexp("^(red-c0823|red-c7228|red-c0831)([.]|$)", Machine) =?= True)
                  || (GLIDEIN_Site =?= "UConn"
                      && regexp("^(nod58)([.]|$)", Machine) =?= True)
                  || (GLIDEIN_Site =?= "Rhodes-HPC"
                      && regexp("^(compute05)([.]|$)", Machine) =?= True)
                  || (GLIDEIN_Site =?= "GREX"
                      && regexp("^(n358)([.]|$)", Machine) =?= True)
                  || (GLIDEIN_Site =?= "ComputeCanada-Fir"
                      && regexp("^(fc30438|fc20637|fc20621|fc20667|fc20624|fc30416)([.]|$)",
                                Machine) =?= True) )
+UNDESIRED_Sites = "UCSD, FNAL, MI-HORUS, GREX, BNL-SDCC, Alabama-CHPC"
periodic_remove = (JobStatus == 2 && (CurrentTime - EnteredCurrentStatus) > 604800)
```

No `+SingularityImage` is set, so the glidein starts the pilot in its
own default image (below). The 2026-08-11/13 revision added the
user-namespace requirement and the undesired-site list to the
2026-03-17 version, and changed the pilot user from `eic` to `epic`.

## The pool as advertised

Slot classads on 2026-09-01 (7410 slots):

| Site | Slots | OS | Unprivileged user namespaces | ATLAS CVMFS |
|---|---|---|---|---|
| SGridGLA | 2534 | RHEL 9 | enabled | yes (28 without) |
| CNAF | 1505 | RHEL 9 | enabled | yes |
| MIT | 1135 | RHEL 9 | enabled | yes |
| UConn | 1065 | RHEL 9 (99 RHEL 8, Debian, Ubuntu) | enabled | yes |
| SU-ITS | 719 | RHEL 9 | unavailable | mostly |
| JLab-FARM-CE | 377 | RHEL 9 | enabled | no |
| UCSD | 71 | RHEL 9 and 8 | enabled | yes |
| GRIF_LLR | 4 | RHEL 9 | enabled | no |

Container policy is uniform: `GLIDEIN_Singularity_Use = PREFERRED`,
`GLIDEIN_SINGULARITY_REQUIRE = OPTIONAL`, image restriction `cvmfs`
on 6968 slots and none on 426, apptainer in unprivileged mode except
SU-ITS (privileged). The image dictionary's default for RHEL 9 is
`/cvmfs/singularity.opensciencegrid.org/opensciencegrid/osgvo-el9:latest`.
PREFERRED with OPTIONAL means a job runs in the default image unless it
names its own with `+SingularityImage`; any image under
`/cvmfs/singularity.opensciencegrid.org` satisfies the restriction.

The current requirements admit 6177 of the 7410 slots. Excluded: SU-ITS
(no unprivileged user namespaces), JLab-FARM-CE and GRIF_LLR (no ATLAS
CVMFS), and the non-RHEL 9 slots. Pool membership changes daily; in the
24 hours to 2026-09-01 16:00 ET the pilots ran at MWT2, Indiana,
Alliance Canada, UConn, Illinois, Wisconsin, UW-Milwaukee, INFN, and
FSU.

## Container layering as implemented

1. The glidein starts the pilot job inside the pool's default image
   (`osgvo-el9`), because the submit description names none.
2. The BNL wrapper runs bare inside it. Its own apptainer step
   (`sing_cmd`, the ATLAS almalinux9 image) is taken only when the CRIC
   `container_type` is `singularity:wrapper`; the ePIC queues carry
   `apptainer:pilot`, so it is skipped. The wrapper does use ATLAS CVMFS
   for its python3 (ALRB).
3. The pilot's epic module (`pilot/user/epic/container.py`) always
   wraps the payload with ALRB: `setupATLAS -c <image>` launches
   `eic_xl` as a second, nested apptainer. Pilot `Container.setup_type`
   is `ALRB` and the epic module has no raw-apptainer path and no
   detection that it is already inside the requested image.
4. The payload runs in the nested `eic_xl`.

The nesting is why the submit description requires unprivileged user
namespaces, and the ALRB launch is why it requires ATLAS CVMFS. Where
either is absent the job cannot run; this is the mechanism behind the
2026-07 failures at sites with user namespaces disabled, and behind the
exclusion of the JLab farm.

## Activity

PanDA record for BNL_OSG_EPIC_PROD_1 on 2026-09-01: 2305 jobs in the
last 24 hours, 2198 finished and 107 failed, about 5300 core-hours, six
production tasks; the seven-day total is 2237 finished against 650
failed, 541 of the failures on 2026-08-26 with nothing finishing that
day. The queue is opportunistic capacity fed only when tasks target
it; it idles for lack of work rather than lack of slots.

The compute-usage page (`/compute-usage/`) shows the queue as
"OSG Pool (BNL_OSG_EPIC_PROD_1)"; its row expands inline into its
execute sites for the selected period, under a header of its own with
an independent sort: jobs, failures, failure rate, core-hours,
efficiency, and share within the pool. Test queues stay out of the page
until the Test queues tick is on. The execute site is
the glidein site the pilot records on the job: since the 2026-08-13
change of the pilot user to `epic`, the pilot's epic module reads
`GLIDEIN_Site` from the glidein's machine ad (`_CONDOR_MACHINE_AD`) and
reports it as the job's `destinationsite` (the submit host as
`sourcesite`). Records that predate the report fall back to the
worker-node host's domain. A job whose host is the submit host never
reached a worker node and is listed as not dispatched. In August 2026
the queue's jobs resolved to 24 named sites, among them UChicago
(MWT2), ComputeCanada-Fir, UConn, GREX, CNAF, MI-HORUS, FSU, CHTC,
Alabama-CHPC, FANDM-ITS, Nebraska, Rhodes-HPC, UWM-Mortimer, LIGO-WA,
and BNL-SDCC. The same breakdown is available through the
`panda_resource_usage` MCP tool with `execute_sites`. The pilot's
condor job on osgsub01 also records the site (`OSG_SITE_NAME` in the
wrapper output, `GLIDEIN_Site` in the schedd history), the source the
maintainer's `count_error_sites.sh` reads.

## Levers

Under production operations control now:

- Submit description: `+SingularityImage` (an `eic_xl` image on CVMFS
  is admissible on every slot), `Requirements`, `+UNDESIRED_Sites`,
  `+JobDurationCategory`, resource requests.
- Harvester queue configuration: worker limits, push versus pull
  (with the CRIC `workflow` field), the template and wrapper used.
- The pilot (pilot3, `pilot/user/epic/`): a stand-down when the
  requested image is already the running container, and independence
  from ALRB.
- The payload and the submission spec on the production side.

Not under local control: the pool's frontend policy and factory
entries. They belong to the JLab VO frontend, whose groups are CLAS12
and GlueX; an EIC group in that frontend, or an EIC frontend of its own,
is what would give ePIC its own glidein policy and site list.

### Queue behavior without a CRIC edit

The BNL pilot wrapper takes its queue data from a `queuedata.json` in
the pilot's working directory when one is present, and only otherwise
from the CRIC cache
(`http://pandaserver01.sdcc.bnl.gov:25080/cache/schedconfig/<queue>.all.json`).
The OSG submit description already ships one file into every job
(`transfer_input_files`); a `queuedata.json` added to that list is in
effect on the next pilot for every pilot-side field: maxtime, container
type and options, copytools, catchall. This is the mechanism the npps0
worker runs on ([PANDA_CAPABILITIES.md](PANDA_CAPABILITIES.md)). CRIC
follows; it keeps the queue's existence and the fields the server and
harvester read. The harvester-side fields (worker limits, push or pull)
are in the harvester's queue configuration on the submit host.

### Pilot selection and the test pilot path

The wrapper runs the pilot named by `--piloturl` when the harvester
passes one (from the CRIC harvester parameter `pilot_url` on the
queue); otherwise it fetches
`/cvmfs/eic.opensciencegrid.org/panda/pilot/pilot3.tar.gz`, or
`pilot3-<version>.tar.gz` when CRIC names a pilot version. A pilot-test
queue exists: `BNL_OSG_PanDA_pilotest`, with its own submit description
and its own copy of the wrapper, which differs only in the pilot
directory, `/cvmfs/eic.opensciencegrid.org/panda/pilot/test/`. A test
pilot therefore runs either from that CVMFS directory or, with neither
CVMFS nor CRIC involved, from a `--piloturl` set in the pilot-test
template and pointing at a tarball served from a host in hand (osgsub01
serves its log directory over HTTPS). The template accepts the
argument; the served-tarball route has not yet been exercised.

That test directory belongs to the PanDA team at BNL and we do not
write to it. Ours is the canary directory below.

Since 2026-10-02 the two ePIC production queues run the canary pilot,
`/cvmfs/eic.opensciencegrid.org/panda/pilot/canary/pilot3.tar.gz`
(then `pilot3-3.14.3.3-epic5`), named by an explicit `--piloturl` in
place of `{pilotUrlOption}` in the submit description's `arguments`:

- `UM_GREX_PanDA_1`: `submit_pilot3_push_umgrex.sdf` on
  pandaharvester01, its own template; backup
  `submit_pilot3_push_umgrex.sdf-backup-2026-10-02`.
- `BNL_OSG_EPIC_PROD_1`: a template of its own,
  `submit_pilot2_push_bnl_osg_epicprod.sdf` on osgsub01, a copy of the
  shared `submit_pilot2_push_bnl_osg.sdf` with the pilot URL set, and
  the queue's `templateFile` in `panda_queueconfig.json` pointed at it;
  backup `panda_queueconfig.json-backup-2026-10-02`.
  `BNL_OSG_PanDA_1` and `BNL_OSG_PanDA_pilotest` keep the shared
  template and its CRIC-driven pilot URL.

Harvester reads the template at every submission and reloads its queue
configuration within ten minutes, so no service action is needed. A
new pilot reaches both queues by moving the canary symlink (below);
reverting a queue is restoring its backup.

### Our canary pilot directory, and publishing to it

`/cvmfs/eic.opensciencegrid.org/panda/pilot/canary/` is the ePIC
production team's own pilot area: where we place a pilot of our own
build — a patched pilot carrying a fix not yet released, or a release
we want to run ahead of the production default — and where the
`pilot3.tar.gz` symlink names the one currently offered. It held
`pilot3-3.14.1.31.tar.gz` and `pilot3-3.14.2.2.tar.gz` as of
2026-09-09, the symlink on the latter.

Publication is ours to perform, on the SDCC write hosts
`cvmfswrite06` and `cvmfswrite07` (the SDCC stratum-zero page,
<https://docs.sdcc.bnl.gov/services/cvmfs/stratum-zero/>). There the
repository is a writable NFS mount of the stratum-zero staging area
(`cvmfsnfs01:/cvmfst0/eic.opensciencegrid.org`, 15 TB), and the canary
directory is owned `wenauseic:eic`; SDCC enabled the account on both
write hosts on 2026-09-04 at our request. The rest of `panda/` is the
PanDA team's, owner `xzhao`, and we do not write there.

**Reaching the write host.** Every route was tried on 2026-09-09 and
exactly one works: ssh from pandaserver02 *through an SDCC gateway*
(`ssh01.sdcc.bnl.gov` or `ssh04.sdcc.bnl.gov`), authenticating with the
ssh key SDCC holds for the account in LDAP, the one that logs in to the
gateways, presented from the ssh agent that a live gateway login
forwards to pandaserver02. The write hosts accept that key only when
the connection arrives from a gateway; the same key offered directly
from pandaserver02 is refused. Not routes: a key in
`~/.ssh/authorized_keys` (the write hosts do not read it), a Kerberos
ticket (the gateway login is by key and issues none), the SDCC key
portal (needs a second factor the account does not have). The
configuration that encodes the route, in `~/.ssh/config` on
pandaserver02:

```
Host cvmfswrite06 cvmfswrite07 cvmfswrite06.sdcc.bnl.gov cvmfswrite07.sdcc.bnl.gov
    ProxyJump ssh04.sdcc.bnl.gov
    IdentitiesOnly no
    StrictHostKeyChecking accept-new
```

The agent is whichever gateway login of Torre's is alive: its socket
is `/tmp/ssh-*/agent.<pid>` on pandaserver02, `ls -t` picks the
newest, and a session started from a login shell inherits
`SSH_AUTH_SOCK` already. Nothing else is needed, and nothing expires
while a login lasts.

**One publication**, as run on 2026-09-09 for `pilot3-3.14.3.3-epic1`:

1. Build the tarball on pandaserver02: extract the released
   `pilot3-<version>.tar.gz` from the production directory, apply the
   change, confirm that only the intended files differ, repack with
   the same top-level `pilot3/` layout, and name it
   `pilot3-<version>-epicN.tar.gz`, `N` counting our patches on that
   base. `PILOTVERSION` inside stays the base version; the name
   carries the patch.
2. Before writing, on the write host: no `CVMFSRELEASE` anywhere in
   the first three levels of the repository, and the `panda/pilot`
   listing there equal to the published mount's, so a publication
   carries nothing of anyone else's in flight; free space.
3. `scp` the tarball into the canary directory and compare its sha256
   there with the source.
4. `ln -sfn <tarball> pilot3.tar.gz` in that directory. The previous
   version stays: a pilot we have run under is never removed, so a
   bad pilot is undone by moving the symlink back.
5. `touch CVMFSRELEASE` there. That file triggers publication; the
   flag must sit within the first three levels of the repository, and
   `panda/pilot/canary` is the third.
6. Watch this host's mount: the new file and the symlink appear, the
   repository revision (`attr -g revision /cvmfs/eic.opensciencegrid.org`)
   advances, and the flag is gone from the write host.

First exercised on 2026-09-04 with `pilot3-3.14.2.2.tar.gz`; second on
2026-09-09 with `pilot3-3.14.3.3-epic1.tar.gz`, the released 3.14.3.3
plus pilot3 PR 220 (NODE_EVENT_DISPATCHER.md): flag at 19:48 ET, the
flag gone within 5 minutes, revision 9739 visible on this host 8
minutes after the flag with the file, the symlink and the checksum as
written.

**What consumes it.** A queue takes the canary pilot only through the
mechanisms above, a CRIC `pilot_url` on the queue or a `--piloturl`
where the wrapper is invoked. One consumer is in this repository:
`BNL_NPPS_GPU`, whose pass script selects the pilot with `--piloturl`
and takes the canary tarball by that route
([NPPS0_TEST_QUEUE.md](NPPS0_TEST_QUEUE.md) § Choosing the pilot);
a canary pilot runs there first. For the
production queues neither mechanism is in this repository.
`NERSC_Perlmutter_epic` in particular carries pilot manager `local`
and an environment pointing into the NERSC project software area,
which is the site's own harvester installation, so on the queue
record alone a canary pilot does not reach Perlmutter; nor does this
directory, since the nodes do not mount `eic.opensciencegrid.org`.
The Perlmutter route is recorded in
[NERSC_PERLMUTTER.md](NERSC_PERLMUTTER.md).

**Every place a pilot of ours goes.** One tarball, built once (step 1
above), is placed in each of these; a pilot is "published" when all
of them hold it. Each has its own consumer and its own route, and none
follows another automatically.

| Place | Consumer | How it gets there | Takes effect |
|---|---|---|---|
| The canary directory, `/cvmfs/eic.opensciencegrid.org/panda/pilot/canary/`, symlink `pilot3.tar.gz` | any queue whose pilot selection names the directory: `BNL_NPPS_GPU` when its pass script's `PILOTURL` is the canary symlink; OSG queues only by a `pilot_url` that PanDA operations sets | steps 2 to 6 above (the `cvmfs-canary-publish` procedure) | the next pilot after the repository revision advances, about 10 minutes |
| The npps0 configuration directory, `~/npps0-config/pilot3.tar.gz` on the host | `BNL_NPPS_GPU` when its pass script runs `PILOTURL=local` ([NPPS0_TEST_QUEUE.md](NPPS0_TEST_QUEUE.md) § Choosing the pilot); the state since 2026-09-09 | a file copy to the host (the `npps0` deploy procedure) | the next pass |
| The public `pilot/` prefix of the devcloud bucket, `https://epic-devcloud-stageout.s3.us-east-1.amazonaws.com/pilot/` ([DEVCLOUD_STAGEOUT.md](DEVCLOUD_STAGEOUT.md) § 4) | the Perlmutter pilot launch, `perlmutter/<queue>/epicprod-perlmutter-pilot-launch.sh`, which fetches the tarball at every pilot start and verifies its sha256 ([NERSC_PERLMUTTER.md](NERSC_PERLMUTTER.md)); later any worker outside the SCDF perimeter | one `aws s3 cp` with the worker profile, then a commit on `main` changing the launcher's two lines, `PILOT_TARBALL_URL` and `PILOT_TARBALL_SHA256` | the next worker start after the commit (the launcher is fetched by the site at every worker start) |

A queue that is to keep running an earlier pilot keeps it by these
same lines: the canary symlink moved back, the host file replaced, or
the launcher's two lines reverted. The tarballs themselves are never
removed from any of the three places.

**Our pilot wrapper.** The canary directory also carries
`epicprod-pilot-wrapper.sh` (source `pilot/epicprod-pilot-wrapper.sh`),
for a queue whose workers should run our pilot with the core count of
the worker they land on. It is written for `BNL_ePIC_GOOGLE_es`,
whose Harvester worker pods are sized by the queue's CRIC settings. It
does two things:
- It exports `ATHENA_PROC_NUMBER`, unless the environment sets it,
  from the worker's CPU quota (a Kubernetes pod's CPU limit, which
  `nproc` does not see), else from the cores the process may run on.
  The pilot sizes its Event Service range fetches by it, and the
  payload's harness takes its slots from it (NODE_EVENT_DISPATCHER.md,
  The core count, from the node).
- It runs the standard `bnlpanda.runpilot2-wrapper.sh` with the
  queue's arguments unchanged and our `pilot3.tar.gz` appended as
  `--piloturl`, which overrides any earlier one.

It also sets the pilot environment our Event Service queues run with,
as the npps0 pass script and the Perlmutter launch do:
- our servers and flavour (`-e eic`, `--url pandaserver01 -p 25443`,
  `--pilot-user epic`, `--rucio-host nprucio01`), placed before the
  caller's arguments so they can be overridden;
- BNL Rucio for stage-out (`RUCIO_CONFIG`, `RUCIO_ACCOUNT`);
- the generic Event Service executor, with yampl from
  `es-channel-py311-el9/` in the same directory;
- the queue's pilot-side configuration from
  `queuedata/<queue>.json` in the same directory, when one is
  published there.

Since 2026-10-01 the directory holds, beside the wrapper:
- `pilot3-3.14.3.3-epic4.tar.gz`, the Perlmutter Event Service pilot
  (pilot3 PRs 220, 221 and 224), now named by the `pilot3.tar.gz`
  symlink;
- `es-channel-py311-el9/`, yampl and its Python 3.11 binding;
- `queuedata/BNL_ePIC_GOOGLE_es.json` (source
  `gke/BNL_ePIC_GOOGLE_es/queuedata.json`): the server's configuration
  for the queue, with the `es_events` and `es_failover` activities of
  Perlmutter's.

The queue points its workers at the wrapper in place of the standard
one; for `BNL_ePIC_GOOGLE_es` the production operations agent starts
those workers itself (GKE_PILOT_FLOW.md). The wrapper is published by
the same steps as a tarball.

### Excluding what delivers nothing

On 24 October at 14:25:04 UTC, the active pull template and 588 idle
native pilots gained a temporary exclusion of the single Fir glidein
`glidein_2038143_212390262` on `fc20524.int.fir.alliancecan.ca`.
Nine workers there were held with code 21, disk usage 4,750,000 KiB
against a 4,500,000 KiB request; those stopped workers were removed by
an owner, queue, held-state, error and exact-glidein constraint. The
exclusion matches the glidein in `TARGET.Name` together with its Fir
site and full `TARGET.Machine` name. Other glideins on that host and
all running workers were retained. This contains that allocation's
refills without declaring the entire host bad or increasing disk
requests; scratch sizing remains a separate investigation. The unique
glidein clause ceases to match when that allocation disappears and can
then be removed. The template backup ends
`.before-fir-disk-20261024T142504Z`; worker evidence and the pre-edit idle
ads are retained in `native-site-runtime-20261024`.

Two levers, at two granularities, both in the submit description that
`BNL_OSG_EPIC_PROD_1` and `BNL_OSG_PanDA_1` share. `+UNDESIRED_Sites`
excludes a site. A `Requirements` clause excludes an individual worker
node, and it names the node together with its site: `GLIDEIN_Site`
identifies the site and `Machine` the execute host. A node is only ever
identified by the pair, because node names recur across sites — the
pool advertises bare names such as `compute05`, `n358` and `fc20621`
alongside fully qualified ones, and a name alone would ban a healthy
machine elsewhere with no indication that it had. The regular
expression anchors each name with `([.]|$)` so it binds whether the
host is advertised bare or fully qualified; both forms occur.

On 24 October 2026 NotreDame was excluded after 2 finished and 133 failed
jobs in six hours and repeated node-guard black holes across its hosts.
`scripts/exclude-osg-site.py --site NotreDame --reason 'EVIDENCE' --apply`
adds the hard exact-site requirement to the active production pull
description and each already-idle production pilot, retaining all other
requirements and a dated template backup. It never edits or removes a
running worker. The same helper is available to the watcher under the
standing operational authority in EPICPROD_WATCHER_BRIEF.md; routine
protective exclusions do not require an operator Poke.

Applied 2026-09-07 on the evidence of thirty days of job records: every
node listed had zero finished jobs and at least fifty failures, and
together they account for 14,480 failed jobs and 9,997 core-hours. The
twelve named nodes sit at sites that otherwise deliver — Nebraska,
UConn, Rhodes-HPC, GREX and ComputeCanada-Fir — which is what makes
node granularity the right instrument for them. Alabama-CHPC is
excluded as a site instead: all nineteen of its nodes appearing in the
record are in the same state, 13,218 failed and none finished, so the
site is the unit and a node list there would need upkeep as machines
appear. The clause was checked against the live pool before it was
written: it excluded the banned host and no other, a healthy node at
the same site survived it, and `condor_submit -dry-run` confirmed the
expression survives submit-file expansion.

Trial 39305 is the case that prompted it: the job simulated and
reconstructed 100 events on `chpc-compute001`, validated them, and then
lost all of it to three consecutive ten-minute timeouts against the
JLab catalog, which that node cannot reach although it reached S3 in
AWS throughout. The payload's landing check now declines such a node in
seconds ([EPICPROD_PAYLOAD.md](EPICPROD_PAYLOAD.md), exit code 80);
exclusion keeps the pilots away from it in the first place.

**The node guard's clause.** Since 2026-10-20 the node guard's black
holes (site-canary docs/NODE_GUARD.md) are kept off the pool's pilots by
the same matchmaking, beside the operators' clause and without touching
it. Both pull descriptions, `submit_pilot2_pull_bnl_osg.sdf` and
`submit_pilot2_pull_bnl_osg_epicprod.sdf`, end their `Requirements` with
`&& (MY.EpicprodNodeGuard =!= False)`, and after the marker line
`# epicprod node guard: osg-node-guard-clause.py writes the next line`
carry the attribute `+EpicprodNodeGuard`: an expression false on every
node the guard's live exclusion names for a `BNL_OSG_` queue, by its full
host name or by its bare name together with its glidein site, and `True`
when it names none. `scripts/osg-node-guard-clause.py` rewrites those two
lines every five minutes as `atlpan` from the document the landing check
reads (`/etc/cron.d/osg-node-guard-clause`; the script, its `COMMIT` and
its log `clause.log` in `harvester_common/node-guard/`). A document that
is not live, or has expired, writes `True`; one that cannot be read
keeps the clause until the validity it was written with has passed, so a
publisher that stops leaves no exclusion standing. Harvester reads the
description at every submission, so the next pilot submitted is never
matched to the node; pilots already idle in the schedd keep the ads they
were submitted with. A node excluded on either OSG queue is kept off
both, since the pool and the payload are the same: on 2026-10-20 the CNAF
node `cn-leo-070.cr.cnaf.infn.it`, excluded on `BNL_OSG_PanDA_1` at 05:19
ET, went on taking jobs on both queues and declined 361 of them at the
landing check in four hours, each at the cost of an attempt.

The clause reaches only new matches, and two gaps showed on 23 October
when the guard tripped nod46 at UConn at 00:59 ET: pilots already idle in
the schedd keep the ads they were submitted with, so their clause predated
the trip and five of them matched the node over the next hour; and a pilot
on the node keeps taking jobs the payload's landing check declines in
seconds, one attempt each. Since 02:00 ET the cron runs the script with
`--refresh-idle`: one constrained `condor_qedit` per cycle sets the current
expression and a stamp (`EpicprodNodeGuardStamp`) on the idle pilots of
these queues whose stamp differs, and the next negotiation uses it (598 ads
refreshed in the first run). `--list-churners` prints the running
pilots on excluded nodes that started at or after the exclusion, the likely
churners, and removes nothing: the operator removes them with the job
evidence in hand, because the landing check proceeds when the guard's
document cannot be read, so a start time alone is not proof that no healthy
job runs. Five such pilots were removed by hand that night. Our own pilot closes the gap from inside: before each further job
it reads the guard's document and takes no further job on an excluded node
(NATIVE_PILOT_CANARY.md, the per-acquisition guard).

Checked before it went live, on osgsub01 against the pool's own slot
ads: with the clause naming two nodes then in the pool, one by full name
(`nod48.phys.uconn.edu`) and one by bare name and site
(`CRUSH-OSG-C7-10-5-211-62` at SU-ITS), the pilot's guard evaluated
false on their 63 and 39 slots and true on a healthy node, and
`condor_submit -dry-run` of the edited description carried the attribute
and the reference. Backups: `submit_pilot2_pull_bnl_osg.sdf-backup-2026-10-20`
and `submit_pilot2_pull_bnl_osg_epicprod.sdf-backup-2026-10-20`;
reverting is restoring them and removing the cron entry.

### Targeting a site

The submit description decides where a queue's pilots may land, so
targeting is per queue, not per task. `+UNDESIRED_Sites` is proven to
bind in this pool (no job at a listed site after the Aug 13 change);
`+DESIRED_Sites` is the pool convention's counterpart, and a
`Requirements` clause on `GLIDEIN_Site` binds by plain matchmaking. A
probe queue of its own, with its template rewritten per probe, sends a
canary probe to a chosen site and can carry its own `queuedata.json`.
The desired-site attribute is to be confirmed by the first targeted
probe.

## Plan

1. Baseline: a canary probe to the queue as configured, recording what
   the pilot sees (site-canary).
2. `+SingularityImage = eic_xl` on the submit description together
   with the pilot stand-down, so the glidein's container is the only
   one. Verified by probe.
3. With nesting gone, drop the user-namespace requirement; with the
   pilot off ALRB, drop the ATLAS CVMFS requirement. Each admits the
   slots listed above.
4. Trial pull mode for the queue.

## 22 October: the fall from 3,000 to 300 running

`BNL_OSG_PanDA_1` ran 3,026 jobs at 15:51 ET and 284 at 23:09 ET, with 600
pilots waiting and about 3,200 jobs activated through the evening. Four
things happened, in order. The first three were ours.

1. 15:02 ET: two by-hand whole-task reruns (41039, 41040) landed on the
   retired `BNL_OSG_EPIC_PROD_1`, dropped from the floor but not from the
   front's queue list. Harvester sent it 600 pilots, and by 17:40 ET it held
   1,170 of the pool's slots while the active queue fell by as much. No
   capacity was lost yet.
2. 16:51 to 18:16 ET: the JEDI job throttler read the two OSG queues as one
   pool, so PROD_1's 5,500 queued jobs saturated it and generation for the
   active queue stopped; its activated count was zero. A pilot that started
   in that window asked twice, a minute apart, was told "No jobs in PanDA"
   and exited: every one of the 265 active-queue pilots that started
   between 16:00 and 17:00 ET and ended within ten minutes did this, and
   each gave its slot back to the pool. The pool split at 18:16 ET
   (`osg:BNL_OSG_PanDA_1;osg_drain:BNL_OSG_EPIC_PROD_1`) restored
   generation within fifteen minutes.
3. 18:00 to 23:20 ET: the 600 PROD_1 pilots waiting in the schedd were older
   than every active-queue pilot, so the pool matched them first. All of
   them started, found no PROD_1 work, and exited within four minutes,
   471 of them between 21:30 and 23:20 ET, taking that many of the
   evening's scarce matches.
4. From about 17:00 ET the pools' owners took the slots back. Our pilots run
   as passengers in the JLab VO's pool and on OSPool glideins provisioned
   for others; the afternoon's slots were those glideins' idle time. At
   23:30 ET the JLab pool advertised 11,064 slots, 9,595 of them claimed:
   8,525 by CLAS12's `gemc`, 874 by GlueX's `mcwrap`, 203 by our pilots. As
   each of ours ended at its two-hour time floor, its slot went to `gemc`.
   At 23:36 ET the matchmaker's own analysis of our oldest waiting pilot,
   ignoring user priority, found 1 willing slot of 3,316 in the JLab pool
   and 29 of 9,247 in the OSPool: 2,509 and 7,782 slots fail our
   requirements, and most of the rest are partitionable remnants under
   4 GB or already running others' jobs.

The first three emptied the active queue two hours early and spent about
470 matches on nothing; the fourth sets the level we are at now. Nothing on
the submit side changed between the ramp and the fall: the templates'
4,096 MB request dates from 00:55 ET and the ramp to 3,026 came after it.
Both collectors list our schedd with its idle count, the JLab frontend reads
our queue with a valid token, and reads need no authentication here, so the
IDTOKENS failures logged from 128.105.82.113 once a minute are a daemon
that cannot authenticate to us for a command that needs it, not a hidden
demand.

Changed as a result, installed by `scripts/configure-osg-regulation.py`:

- The active queue's pilots hold their slot through a dispatch gap:
  `--getjobrequests 30 --getjobfailures 30` in
  `submit_pilot2_pull_bnl_osg.sdf`, in place of the pilot's defaults of 2
  and 5, so a pilot asks for up to thirty minutes before it gives the slot
  back. The wrapper passes both through unchanged. Pilots already waiting
  keep the arguments they were submitted with.
- `BNL_OSG_EPIC_PROD_1` is `offline` in Harvester's queue configuration. It
  submits nothing again, whatever is activated on it.

What would raise the level is capacity provisioned for us rather than
passenger slots. The OSPool frontend provisions glideins for a flocked
schedd's idle jobs only when it can match their requirements to its entries;
`HAS_CVMFS_atlas_cern_ch` and the user-namespace requirement cannot be
matched, and both exist only for the ALRB-nested pilot. The native pilot that
passed its canaries on SU-ITS and GREX on 22 October needs neither (plan
steps 2 and 3 above); its staged trial on the active queue began on 23
October ([NATIVE_PILOT_CANARY.md](NATIVE_PILOT_CANARY.md)). The memory request is the next filter: at 23:36 ET
4,096 MB admitted 500 of 2,384 otherwise fitting OSPool slots and 206 of
2,125 in the JLab pool.

Verified in the field at 06:40 ET on 23 October: the first ordinary
production pilot submitted with the new arguments to end, 26692.1 (wall
7,590 s, exit 0), logged `getjob_requests=30, getjob_failures=30` in its
Namespace line. The pilots submitted before 00:53 ET keep the old 2/5 until
consumed.

## Related

- [EPICPROD_OPS_AGENT.md](EPICPROD_OPS_AGENT.md): the production
  operations agent, which will run the submit-side changes.
- [PANDA_CAPABILITIES.md](PANDA_CAPABILITIES.md): worker provisioning
  on the other queue classes.
- site-canary `docs/PLAN.md`: the probe used to measure each step.

## JLab startup repair, 22 October 2026

JLAB_PanDA_1 was routing and starting workers while our monitoring claimed
it was waiting for CE routing. Worker 993277 (Condor 26976.0, CE 1194528.0,
Slurm 13384333) started on farm180226 at 14:00:33 UTC, 38 seconds after
submission, with **2000 MB**. Our wrapper then exited 64 because it tried
to source ATLAS Local Root Base, absent on that worker. The ePIC pilot
archive itself was readable. Harvester's later discovery timestamps must
not be interpreted as actual Slurm start times.

`pilot/jlab-native-pilot.sh` uses the standard wrapper's `--localpy`
option and native middleware. It resolves the transferred pilot proxy
before the standard wrapper changes directory. The JLab-only installer
`scripts/configure-jlab-native-pilot.py` changes its submit template to
transfer that proxy and the standard wrapper as inputs and disables GSI
proxy delegation. The CE still authenticates with its existing SciToken.
The proxy is for pilot-to-PanDA authentication and is not printed. This
leaves all other queues and their wrappers unchanged. Apply as atlpan on
pandaharvester01, with `--wrapper <shipped jlab-native-pilot.sh> --apply`;
restore the dated `before-native` template backup to roll back. Harvester
reads the template at submission; no shared service restart is needed.

A separate bounded diagnostic, `pilot/jlab-startup-probe.sh`, was submitted
as Condor 26993 / CE 1194817 on a 2000 MB allocation. Slurm 13405132 ran
on farm180118 at 16:02:55 UTC. It confirmed Python 3.9.25, psutil, requests,
xrdcp, Apptainer, ePIC CVMFS and readable valid proxy delivery without GSI
delegation. ATLAS CVMFS was absent. Native Python has neither Rucio nor
XRootD modules; payload/copytool requirements still need an actual canary.
IPv4 BNL Rucio and JLab Rucio returned HTTP 200; PanDA TCP 25443 timed out
and TCP 443 refused the connection. IPv6 was unusable. Locating the
25443 block and proving a complete canary remain part of commissioning.
