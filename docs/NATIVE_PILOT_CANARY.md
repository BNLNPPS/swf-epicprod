# Native ePIC pilot and single-container canary — 22 October 2026

This is an isolated candidate, not a production pilot release. Torre
authorized building and testing the replacement on operating queues, with
enough priority to be picked up. Production promotion and removal of
production exclusions remain his decision.

The corrected candidate finished successfully on both GREX and OSG's
SU-ITS site. It starts through the ePIC image's Python, reuses that image
for the payload, preserves PanDA token exchange, and delivers the pilot
log through the queue's declared protocol. The failures encountered and
their corrections are recorded below; no production pilot, exclusion,
queue configuration, or server was changed by these tests.

## What changes

`pilot/native-canary/epic-native-canary.sh` starts the pilot using Python
and its dependencies from the requested ePIC campaign image. On OSG the
submit description requests that image with `+SingularityImage`. Pilot
and payload share it. A native batch allocation launches the same image
once through Apptainer; an explicit OSG CVMFS runtime is the fallback when
the executable is absent from `PATH`.

The pilot's ePIC container wrapper reuses an already matching image and
refuses a different image. It preserves the production multistep
`containerExec` substitution. The pilot memory monitor uses the image's
`prmon`. The replacement launcher does not source ATLASLocalRootBase or
run the old `lsetup` bootstrap.

This uses the facility's supported container selection rather than a
second container inside its default image. See the [OSG container
exercise](https://osg-htc.org/user-school-2022/materials/software/part3-ex1-singularity/)
and [HTCondor container execution documentation](https://htcondor.readthedocs.io/en/v26_0-branch/admin-manual/ep-policy-configuration.html).

## Preserve the actual deployed baselines

The active OSG queue and GREX use different archives. The candidate is
built separately from each; applying a patch to the reference clone would
not establish what was changed in either deployed pilot.

| Queue | Baseline | SHA-256 |
|---|---|---|
| BNL_OSG_PanDA_1 | `pilot3-3.14.3.3.tar.gz` | `5fa3371510991b4ef059c9a47feb68b6cb780fca90e606734d99aa1b3c1e41e2` |
| UM_GREX_PanDA_1 | `pilot3-3.14.3.3-epic5.tar.gz` | `43a5c43838b0371014bb3dd10208d20f69d4edf1c8e08928cd18cd6306eb4144` |

The OSG archive is under `/cvmfs/eic.opensciencegrid.org/panda/pilot/`;
the epic5 archive is under its `canary/` directory. That directory's
`pilot3.tar.gz` pointer is consumed by production. **Its name does not
make it a safe place to substitute an experimental archive.**

The corrected candidates submitted as tasks 41025 and 41033 have these
archive hashes (the common launcher is transferred separately):

| Candidate | SHA-256 |
|---|---|
| OSG | `6a38d453b014fb24c9d9b2dd8a42ad844887d6d530a50dbd98ae73f00a34c283` |
| GREX epic5 | `eface4ae08bd551d0ec12da4d7ab2ca683f9908dbf28f3080b0c18fdd212214f` |

The builder verifies the input hash, requires a new output directory,
records each changed file, and emits a deterministic archive. Example:

```sh
python3 pilot/native-canary/build.py \
  --baseline /cvmfs/eic.opensciencegrid.org/panda/pilot/pilot3-3.14.3.3.tar.gz \
  --sha256 5fa3371510991b4ef059c9a47feb68b6cb780fca90e606734d99aa1b3c1e41e2 \
  --output /path/to/new-canary-build
```

Five pilot files change: the ePIC container wrapper, the new native
container module, the ePIC memory-monitor setup, and a task-specific test
guard in job acquisition, plus an explicit pilot-log upload scheme in the
Rucio copy tool. `pilot/util/https.py` is identical to its
baseline, including authentication and token refresh.

## Credentials and configuration

OSG and GREX currently use the pilot's robot proxy to authenticate the
payload's PanDA token exchange. The payload saves that credential as
`PILOT_X509_USER_PROXY` before pointing `X509_USER_PROXY` at its JLab
credential. PanDA then issues the Rucio and storage tokens. Job 5767002,
previously described as an OIDC-token test, used this proxy-authenticated
exchange instead; swf-2 confirmed it from that job's pilot log.

The candidate retains the proxy at a stable readable path. Credential
directories, rather than individual refreshable token files, must remain
visible across any container boundary. The tests use credential `panda`
at payload submission: its expired placeholder cannot supply a working
JLab-proxy fallback. A passing token test requires the payload's
`rucio auth: token from PanDA` message and successful registration.

The first GREX full test exposed a batch-adapter rewrite of
`X509_USER_PROXY` to the CE's private spool. That path was unavailable
inside the image. The launcher now explicitly selects its transferred
`pilot-proxy`, verifies readability before launching, and retains it as
`PILOT_X509_USER_PROXY`. This is the same launcher on both queues.

The first OSG full test exposed a separate log-registration failure. The
image's Rucio upload client selected `davs` for `BNL_PROD_DISK_1`; upload
and checksum verification succeeded, but PanDA's adder rejected that PFN
because the RSE's DAVS read/write priorities are zero. Both queues'
declared pilot-log write protocol is `root`. The isolated launcher sets
`EPICPROD_CANARY_LOG_SCHEME=root`, and the candidate copy tool passes it
as Rucio's `force_scheme` for log files only. It does not change payload
uploads, the RSE, CRIC, or the server.

Each isolated submission receives the queue's current configuration as
files, with `timefloor=0` for one test job. Full queue and storage data are
also supplied by file. Storage data for these tests comes from the same
datalake CRIC endpoint used by the current wrapper. These test files do
not change the production synchronization or overrides.

## Test record

The source payload configuration is
`group.EIC.26.07.3.epic_craterlake.p5157.e69.s11.r11`, using
`/cvmfs/singularity.opensciencegrid.org/eicweb/eic_xl:26.07.3-stable`.
Test tasks have PanDA priority 2000; isolated test pilots have Condor
priority 100, compared with the ordinary pilots' priority 1.

| Test | Identifier | Observed result |
|---|---|---|
| Ordinary-pilot GREX control | task 41013, job 5782463 | Finished; canary collector healthy, all checks passed |
| Ordinary-pilot OSG control | task 41014, job 5782464 | Finished; canary collector healthy, all checks passed |
| OSG environment, unrestricted eligible site | Condor 26052.0 | Exit 0; requested ePIC image and Python dependencies verified |
| OSG SU-ITS environment | Condor 26053.0 | Exit 0 at 17:13:52 UTC; requested ePIC image, Python 3.13.12, pilot dependencies imported |
| GREX environment, first attempt | Condor 27001.0 | Exit 1 before diagnostics; not a pass |
| GREX environment, explicit runtime | Condor 27002.0 | Runtime started; entry script outside the mounted work directory, exit 127 |
| GREX environment, script bind corrected | Condor 27004.0 | Exit 0 at 17:40:24 UTC; requested image and dependencies verified |
| OSG SU-ITS full candidate, first attempt | task 41016, job 5782775, Condor 26086.0 | 99/99 events simulated and reconstructed; token exchange and output registration passed. Pilot finished at 17:55 UTC; PanDA failed log registration with DDM error 200 (DAVS PFN) |
| GREX full candidate, first attempt | task 41017, job 5782776, Condor 27007.0 | 99/99 events simulated and reconstructed. Registration, log upload, and status updates failed because the CE-spool proxy path was unreadable. Payload exit 78; pilot error 1137 |
| GREX candidate, proxy and log-protocol corrections | task 41025, job 5785644, Condor 27016.0 | PanDA finished at 18:17:48 UTC; pilot and DDM errors zero. Collector: healthy, 99/99 events, all eight checks true including output availability |
| OSG SU-ITS candidate, log-protocol correction | task 41033, job 5787917, Condor 26160.0 | PanDA finished at 18:37:46 UTC; payload, pilot, and DDM errors zero. Collector: healthy, 99/99 events, all eight checks true including output availability |

Intermediate test tasks 41015 and 41030 were picked up by ordinary production
pilots and are not candidate evidence. Full candidate jobs are now reserved
through the ordinary `pilot/acquire_jobs` API with an exact test-task
selector, then handed to the isolated pilot as `pandaJobData.out`.
The request timeout must exceed ten seconds: PanDA subtracts ten seconds
internally before attempting selection.

During reservation, a new test task is temporarily set to priority 1 so
ordinary pilots prefer the production work already available. As soon as
the exact test job is acquired and saved, its task is raised to 2000.
The isolated batch submission has priority 100 throughout. This resolved
the observed race with ordinary OSG pilots during the production ramp.

The full candidate submissions are one core, 3700 MB, and bounded in
runtime and queue residence. They preserve the production undesired-site
list and node exclusions. Only the new OSG test submissions omit the
ATLAS CVMFS, host RHEL9, and unprivileged-user-namespace requirements.
Both corrected full payloads finished and passed all eight collector
checks, including output availability.

Local checks in the actual image, with ATLAS CVMFS hidden, passed pilot
imports/help for both baselines, matching-image reuse, rejection of a
different campaign image, production multistep substitution, and visibility
after atomic credential replacement. Three focused repository tests pass.
The review build's modified source files match the submitted candidates.

Both queues' returned pilot stdout confirms matching-image reuse, the payload's
`rucio auth: token from PanDA` message, `force_scheme: root` for the pilot
log, and pilot exit 0. The private returned stdout hashes are:

| Job | SHA-256 |
|---|---|
| GREX 5785644 | `5c573bc40bebd80edf7b6dded1406ee7c35f44bc01f7a5bd8a264fc8c88a6906` |
| OSG 5787917 | `2f592926ab4c5f0120957bae3c9b8fabc44979ee1091d841776beb316362b39b` |

Full worker logs are not committed because they can carry environment credentials.

The final records are [GREX job 5785644](https://epic-devcloud.org/prod/panda/jobs/5785644/)
and [OSG job 5787917](https://epic-devcloud.org/prod/panda/jobs/5787917/).
The [canary runs page](https://epic-devcloud.org/prod/canary/probes/)
records their collected payload checks.

## What this establishes and what remains

SU-ITS advertised unavailable unprivileged user namespaces and was
rejected by the production requirement. Its environment probe demonstrates
that requesting the ePIC image directly can reach such resources. The
corrected full test completed physics, registered its output and pilot
log, and reached PanDA's finished state. These tests do not quantify
additional pool capacity.

Not every exclusion is caused by nesting. The six named undesired sites,
static host exclusions, and live node guard have mixed reasons. They
must be examined individually; no wholesale removal is justified by one
container test. The JLab worker-to-PanDA port 25443 connectivity problem
also remains independent of the bootstrap change.

Before a general pull-pilot rollout, image selection must agree with
job dispatch. A pilot already inside one image cannot accept arbitrary
jobs for another image. The candidate refuses that mismatch; it does not
yet implement the production policy for servicing multiple campaign
images while retaining timefloor and repeated jobs. The pilot's current
acquisition API has a task selector but no container-image selector.
These were single-job canaries; they do not establish repeated-job or
long-lifetime behavior. A rollout must choose the campaign image before
batch placement and keep subsequent dispatch within that image, or use
a native-host pilot where the facility permits it. It must not blindly
pin a general pull pilot to one campaign: task 41024, announced during
these tests, uses 26.07.1 while these canaries use 26.07.3.

Other operating paths, including Perlmutter, multipilot allocation
lifetimes, and Event Service, need their own bounded checks before this
launcher replaces theirs. The ePIC image's Python is 3.13.12, so real
pilot execution is required beyond import tests. Event Service's channel
extensions must match that Python; an existing Python 3.11 extension
cannot simply be carried over.

The coherent cleanup proposed here is the ALRB Python/bootstrap path,
its redundant package setup, and its unrelated ATLAS repository checks.
Do not discard certificate roots, proxy/token handling, registration,
monitoring, or log delivery merely because inherited names mention ATLAS.

## Staged trial on the active OSG queue, 23 October

Torre's order (01:08 ET): run the native pilot on `BNL_OSG_PanDA_1` for
experience to evaluate in the morning, by the staged sequence swf-3
recommended and he confirmed. Stages, each only after the previous one is
clean: (1) one pilot in `eic_xl:26.07.1-stable`, the image of every task
activated on the queue that night, proving two consecutive jobs in one pilot
and their output and log registration; (2) one pilot in
`eic_xl:26.07.3-stable`, whose 26.07.1 job exercises the nested launch; (3)
twenty pilots; (4) about two hundred. Ordinary Harvester pilots run
throughout. On trouble no new trial submission is made and healthy running
pilots finish.

What runs:

- `pilot/native/epic-native-pilot.sh`: the launcher. It verifies it is inside
  the image `+SingularityImage` asked for, keeps the transferred proxy for the
  payload, sets the certificate roots, Rucio configuration and the `root`
  pilot-log scheme, extracts the archive and runs `pilot.py` with the
  production arguments.
- The archive from `pilot/native-canary/build.py --production`: the tested
  canary without its one-task test guard, four changed files
  (`native_container.py` new, `container.py`, `utilities.py`, `rucio.py`),
  built on osgsub01 from the OSG baseline `pilot3-3.14.3.3.tar.gz`: SHA-256
  `6d8a761d0ae41a9ce83c7a01ec30812118efcbfda5b7aa96f1a59fc82dd54f8d`
  (`/var/data/atlpan/native-20261023/manifest.json`; the same build under
  another Python differs only in tar header format). `native_container` runs
  a job for another image nested in that image instead of refusing; that
  needs unprivileged user namespaces, which the requirements keep.
- Nesting from inside the image, found by swf-3's probes on swf-testbed and
  pandaserver02 (`/data/swf-tmp/epic-native-verify-20261023/`) and
  reproduced on osgsub01: the OSG pool's Apptainer on CVMFS must be invoked
  through its wrapper, `/cvmfs/oasis.opensciencegrid.org/mis/apptainer/1.4.5/bin/apptainer`,
  which supplies the libraries its binary needs (`libsubid.so.3` and others
  absent from the image); and inside the outer container the relocatable
  runtime loses its own path after entering its namespace and falls back to
  `/var/lib/apptainer/mnt/session`, which the image does not have. No glidein
  binds a job's directory there: upstream `singularity_lib.sh` takes binds
  from the glidein configuration only and passes the job's environment
  inside, and the first workers proved it (PSU-LIGO, exit 70; MWT2, effective
  binds `/srv,/etc/hosts,/etc/localtime,/cvmfs,/etc/OpenCL/vendors`). So the
  nested launch makes the directory itself: it runs in a private user and
  mount namespace (`unshare --user --map-current-user --keep-caps --mount
  --propagation private`) with a tmpfs over `/var/lib` holding the
  directory, visible to that launch alone; the runtime's wrapper then nests
  the job's image as usual. Both image directions pass on osgsub01, with the
  pilot's uid kept and writes through the work-directory bind. A local copy
  of the runtime tree, the 1.3.6 build and shipped host libraries do not
  solve it. The launcher takes no job unless a trivial nested execution in
  the other campaign image succeeds through the same construct within two
  minutes (`EPICPROD_NATIVE_PREFLIGHT_IMAGE`; exit 71 with the worker's
  evidence otherwise), and logs the archive's checksum before extracting
  it, so every pilot's log names its archive. A `+SingularityBind` of the
  CVMFS session directory stays in the description as a harmless extra.
- Two modes of the same archive, chosen by the submit description. Nested
  (`EPICPROD_NATIVE_NESTING=on`, the default): a job for another image runs
  nested, which needs unprivileged user namespaces, so the description
  requires them and preflights the other image. Direct
  (`submit-native-cohort.py --direct`, `EPICPROD_NATIVE_NESTING=off`): the
  pilot serves its own image only under task binding, refuses any other
  image before the payload with a plain message, runs no preflight, and
  requires of the site only `HasSingularity` and the image's repository.
  Torre, 02:5x ET: a fallback's requirement must not be make-or-break for
  mainline running, and what else do we require of a site that may be as
  dumb? Audited: the payload reads nothing from CVMFS (it ships its own
  `rucio.cfg` and takes the CA directory from the environment); the pilot
  needs the CA bundle, which the image lacks, and `rucio.cfg` for its log
  upload, and the oasis repository otherwise served only the nesting runtime.
  So the submit script packs osgsub01's maintained `/etc/grid-security/certificates`
  (43 MB) and `rucio.cfg` into every submission and the launcher uses the
  shipped copies, CVMFS copies being the fallback; the direct description
  carries no bind. Left as inherited, for a decision with evidence:
  `UNDESIRED_Sites`, the 9/7 node bans (the live guard now does that job),
  4,096 MB, 4.5 GB, `+MaxRuntime` 360 and `JobDurationCategory` Medium. The
  ad carries `EpicprodNativeMode`.
- The node guard from inside the pilot: the archive carries the payload's
  `landing_check.py` as `pilot/user/epic/landing_check.py`, unchanged, and
  the baseline hook `pilot.user.epic.common.allow_timefloor`, which the pilot
  calls before each further job, reads the guard's document through it and
  returns False on an excluded node; the pilot then finishes the job it runs
  and exits ("timefloor is set to 0"). The hook does not cover the first job,
  so the launcher reads the same document before `pilot.py` and exits 72 on
  an excluded node. A document that cannot be read allows the job, as the
  landing check then decides.
- `scripts/submit-native-cohort.py`: derives the cohort's description from
  the live production template (exclusions, undesired sites, node-guard
  clause, memory, disk, duration category, running bound), with
  `HasSingularity` required, the ATLAS CVMFS and host RHEL 9 requirements
  dropped, priority 2 above the production pilots' 1, and the pilots bound to
  one task (`PanDA_TaskID` with `--allow-same-user=True`, a baseline pilot
  feature: every acquisition then carries the task id). The tag
  `epic-native-20261023` is in `PANDA_JSID` (the job's `schedulerid` from its
  first status update), in `GTAG` (the job's `pilotid` carries the stdout
  URL) and in the ad attribute `EpicprodNative`.

On osgsub01 everything lives in `/var/data/atlpan/native-20261023/`
(launcher, build inputs, archive, `manifest.json`, the generated descriptions)
with stdout under `condor_logs/native-20261023/`. Task 41043 was the bound
task: 1,700 activated jobs at 01:20 ET, image 26.07.1, median job 82 minutes.

Reading the result: PanDA jobs with `schedulerid = 'epic-native-20261023'`,
their status, registration and error fields; `condor_history -constraint
'EpicprodNative == "epic-native-20261023"'` for each pilot's wall time, exit
code and site; each pilot's stdout for its sequence of "processing PanDA job"
lines, "nested preflight ... ok", "reuse matching image" or "launching the
job image nested", and the pilot's final exit; a Condor exit code of 70 or 71
is the launcher refusing to take a job because the bind was not honored or
the preflight failed. Stage 1 is Condor cluster 26834.0 and stage 2 is 26835.0, submitted
02:30 ET with archive
`417334ee81a211af604ff98b25e3b70f3726b1f31cb3157cd8406bff54a584a8`.
Earlier submissions that night: 26746.0 and 26748.0 (stage 1, superseded
builds, removed while idle); 26754.0 and 26755.0, held by the worker's
image-preparation hook on nodes without CVMFS (above); 26759.0, which ran
at PSU-LIGO and exited 70 when the bind it relied on was not there; 26760.0,
26792.0 and 26796.0, replaced while idle; 26797.0, which ran at MWT2 with
the intermediate launcher and recorded the worker's bind evidence; 26802.0
and 26804.0, which ran at CNAF through launcher, namespace preflight and
pilot start in three minutes and then looped for twenty on a defect of the
baseline pilot: it sends the bound task id from `PanDA_TaskID` as a string,
and the server's typed API refuses every acquisition ("'task_id' must be
of type int | None, got str", 35 refusals in `panda-api_pilot.log`). The
build now casts it; the upstream pilot bug is to be reported.

### Results through 04:33 ET, and the cohort of twenty

- Stage 1, pilot 26834 at CNAF, image 26.07.1 in place: job 5816241 finished
  04:29:50 ET, 1,132 events, output and log registered, every error code
  zero; the pilot then took job 5816695, the repeated-job behaviour with it.
  Its pre-payload step lasted seventeen minutes; the cause waits for the
  pilot's stdout.
- Stage 2, pilot 26835 at ComputeCanada-Fir, image 26.07.3 nesting its
  26.07.1 job: job 5816250 finished and registered. The nested launch through
  the private namespace works on a real worker.
- Direct pilots 26875 (CNAF) and 26891 (Rhodes-HPC) acquired jobs 5816380 and
  5816416 three minutes after starting; the GREX-pinned 26893 ran on n351
  through the JLab pool (route evidence only, we already run GREX on our own
  queue); site probes 26902 SU-ITS, 26903 IRISHEP-SSL-UCHICAGO and 26904
  UNL-PATH test the slots the old requirements excluded.
- Torre, 03:4x ET: "keep watching, go to 20 if success in current round
  warrants it", then "1 successful job is validation, repeated jobs is gravy":
  the gate was one direct same-image job finished with registered outputs,
  met by 5816241. Cluster 26957, twenty direct pilots, image 26.07.1, task
  41043, was submitted 04:32:32 ET; swf-3 reads the evidence.
- The willing-slot census (`scripts/osg-mismatch-census.py --exact`) runs
  every ten minutes on osgsub01 under wenauseic's crontab, recording to
  `/eic/u/wenauseic/osg-census/census.jsonl`: the pool's own closures, the
  free and open slots, what each of our clauses turns away, and the
  matchmaker's two-sided willing count in slots and cores for the production
  and the direct pilot. At 04:00 ET: production 10 cores willing, direct 275.
- 04:4x ET: Fir's glideins died under 48 production pilots in two batches
  (Condor "Job was evicted, Code 1005, Unspecified job interruption" at 03:44
  and 04:22 ET; fc30311 alone shed twelve); Condor restarted the pilots
  elsewhere and the PanDA jobs they held became orphans for the heartbeat
  timeout. A pilot does not know its glidein's death time, so it starts jobs
  it cannot finish: the baseline refuses a further job only below a fixed
  1,800 s by the smallest of its clocks (proxy, queue maxtime, HEPiX
  MachineFeatures shutdowntime, the last read from the directory named by
  `MACHINEFEATURES`), takes its first job regardless, and its monitor stops
  the payload 600 s before a known shutdowntime. From archive
  `f7222cb7` onward, `pilot/native/glidein-deadline.sh`, sourced by the
  launcher at every execution (a Condor restart brings a new machine ad),
  reads `GLIDEIN_ToDie` from this execution's machine ad, takes the earlier
  of it and a site's own shutdowntime, writes it as the pilot's
  MachineFeatures shutdowntime beside the site's other feature files, and
  the launcher declines to start (exit 73) with less than the minimum left;
  no usable deadline leaves the pilot's own clocks in force. The minimum,
  applied by the launcher before the first job and by the pilot before each
  further one, is 9,000 s: the job's p99 of 7,200 s plus the monitor's 600 s
  grace, 900 s for stage-out and registration and 300 s of setup; a positive
  integer `EPICPROD_MIN_TIME_FOR_NEW_JOB` in the submission overrides it,
  anything else falls back. swf-3's review found the first cut's three
  defects (a failing grep under pipefail, the site's deadline overwritten,
  a two-hour floor with no room for the stop and the delivery); the tests
  in `tests/test_native_launcher_deadline.py` cover absent and invalid ads,
  an earlier site shutdown, a stale generated deadline after a restart, the
  threshold boundary and the override. The cohort of twenty runs the
  earlier archive; later submissions carry this.
- 04:55 ET: nineteen of the twenty hold one job each of task 41043 (PanDA ids
  5816705 to 5816750, started 04:33 to 04:42 ET), across CRUSH, UA-LR,
  IRISHEP, UNL-PATH, UWM, OU, CNAF, MWT2, UWL and FSU slots, most of them
  OSPool slots the production pilot's requirements never matched; the server
  log shows one request and no empty answer per node. The twentieth, 26957.18,
  is held on scigrid7.physics.fsu.edu by the HTCondor starter's test of our
  image ("Singularity test failed: WARNING: group: unknown groupid 9000",
  hold code 45), the only such hold tonight among every pilot we run; the
  same site's scigrid5 runs proc 17; production pilots carry no image at that
  layer and ran on scigrid7 at 04:27 ET. It stays held as evidence: no
  release, no retry, no site exclusion. Four native jobs have finished
  tonight, each with 1,132 events and zero error codes: 5816241 (26834,
  CNAF), 5816250 and 5816534 (26835 at Fir, nesting the other image, two
  jobs in one pilot), 5816423 (26893 at GREX). Native pilots held 26 running
  jobs at 04:55 ET. The jobs of a native pilot are found in the PanDA
  database by a `pilotid` carrying `native-20261023`, the cohort's log path.

