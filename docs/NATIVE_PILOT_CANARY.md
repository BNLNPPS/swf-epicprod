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
