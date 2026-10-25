# Pilot flow on Google Cloud: BNL_ePIC_GOOGLE_es

The production operations agent manages the pilot flow of the Event
Service queue `BNL_ePIC_GOOGLE_es` itself: it starts the pilot pods in
the GKE cluster, sizes each to a whole node, and keeps their number in
step with the queue's Event Service backlog. PanDA's Harvester does not
serve this queue. `BNL_ePIC_GOOGLE` stays with the PanDA team's
Harvester (`BNL_harvester_2`).

Torre resumed Google ES commissioning on 24 October 2026: at most five
concurrent ES jobs, using the production native pilot. Evaluate natural
preemptions and the recovery of completed/unfinished ranges. Controlled worker
termination or a request to another person requires Torre's explicit approval.
Expansion also requires his explicit approval.

Bounded real-production ES samples use the normal PCS submission path and their
own sample identity. Each whole task has a fixed event budget, is placed wholly
on Google, and retains normal RECO output without a trial expiry. The controller
cap limits concurrent workers independently of that budget. A 26.07.3 sample
must use the matching native image and fresh advertised capability; validation
with 26.07.1 does not establish that worker result.

Decided with Torre on 2026-09-30, after Harvester was found starting
empty workers for the queue (below). The request to the PanDA team
went out the same day.

## Why the agent, not Harvester

Harvester sizes a queue's workers from its CRIC settings. On
`BNL_ePIC_GOOGLE_es`, an mcore queue, every worker is a 16-core MCORE
pod, and its pilot asks for MCORE jobs. PanDA hands out only jobs of
the resource type a pilot declares, as a hard filter (panda-server
`job_complex_module.py`, `getJobs`, `resource_type=:resourceType`).
A 1-core job therefore never reaches those pilots. Harvester keeps
starting pods for it regardless, since in pull mode it starts workers
in step with the queue's activated jobs (harvester
`worker_adjuster.py`).

On 2026-09-30, 45 workers started between 16:30 and 21:53 UTC and ended
empty, one every few minutes, for a single 1-core test job (task 40381).

What a pilot needs from the PanDA server is only a production role.
`acquire_jobs` checks nothing else (`pilot_api.py`). The queue's
`harvester` and `pilot_manager` fields are never consulted when jobs
are handed out, and a pilot that declares no resource type is given
work of any type. Our npps0 test queue `BNL_NPPS_GPU` has run its own
pilots this way since September (NPPS0_TEST_QUEUE.md). So the agent
can run this queue's pilots without Harvester and without the resource
type question.

## The cycle

`gke_pilot_cycle` is a doer of the production operations agent
(`swf_epicprod/gke_pilots.py`, run by swf-monitor
`scripts/gke-pilot-cycle.py`), enqueued by cron every five minutes.
Each cycle:

1. **Reads the backlog.** It counts the queue's activated jobs from
   PanDA's `jobsactive4`, each a job waiting for a pilot.
2. **Reads the supply.** It lists the pilot Jobs it has started in the
   cluster, by label. Live Jobs count even before their pods appear;
   terminating pods still count after their Job is deleted.
3. **Decides** (`decide`, a pure function). The pods to start are the
   activated jobs not already covered by a waiting pod, bounded by the
   cap `max_pods` less every worker alive. With no backlog nothing starts.
4. **Starts them.** Each is one Kubernetes Job, one pod, from the pod
   template, and runs our pilot wrapper in pull mode.
5. **Records** a `gke_pilot_cycle` action when it starts pods or its
   reason changes.

Finished Jobs remove themselves (`ttlSecondsAfterFinished`). A pilot
that finds no job exits after its `--getjobrequests` attempts.
Cycles share a local file lock under `SWF_TMP_DIR` (default
`/data/swf-tmp`), so overlapping cron/manual invocations cannot each
allocate the same remaining capacity.

## The pod

The template is `swf_epicprod/gke/pilot-job.yaml`. The site-specific
parts are in it: the image, the CVMFS volumes and the node pool. They
are to be filled in from the cluster once the agent has its credential.
The cycle sets:
- the name and labels;
- the CPU and memory, the `cpu` and `memory` settings, sized so that
  one pod takes one node;
- the proxy, mounted from the Secret the cycle keeps current;
- the command: the production native launcher and archive, copied from
  `native_runtime`, with the queue, credentials, campaign image and
  Python-matched ES channel supplied as settings. There is no resource-type
  filter: a whole-node harness can be carried by a generated SCORE job.

The controller sets `ATHENA_PROC_NUMBER` from the pod's CPU limit. The
native launcher enters the campaign image and runs its Python, adding
the ES channel after image startup. The
payload's harness takes its slots from the same number
(NODE_EVENT_DISPATCHER.md, The core count, from the node).
Runtime files are copied without preserving the source directory's
ownership or timestamps: Kubernetes owns the destination volume root,
and preserving those attributes aborts setup before the pilot starts.
If the grid pod lacks `/var/lib/apptainer/mnt/session`, the shared native
batch launcher supplies it in a private user/mount namespace, using the
same mechanism as its nested-image path. Existing session directories
are used directly.

Assemble each runtime with `scripts/stage-native-runtime.py --baseline FROZEN_ASSETS
--output NEW_DIRECTORY --archive-sha256 APPROVED_DIGEST`. All launcher and helper
code comes from one committed source revision; only the pilot archive, CA bundle
and Rucio configuration come from the frozen baseline. The manifest records that
revision and every file digest. Never reuse helpers from an older bundle: the
first Google startup reached the campaign image but failed because an old
`runtime-policy.py` lacked the launcher's `--prepare-queuedata` interface.

Publish the new directory under `/panda/pilot/canary` on the CVMFS write host,
then update `gke_pilots.native_runtime` to its immutable CVMFS path. Existing
bundles are retained. The controller holds launches until the path is available.
The approved GREX production archive has SHA256
`9532f30b41a8b1c8cfcb730eb325933c71c486454ed4365cd3e2d8b625a27ae2`.
This is one native pilot implementation. ES fixes belong in that shared
implementation; commissioning tasks and configuration remain separate.

Each successful controller cycle publishes `gke_pilot_runtime`, a cached
observation carrying the checked archive digest, runtime path, pod-template
hash and bound image. It also carries pending/running worker counts, the latest
worker phase and startup diagnostic, retained failed Jobs, the configured cap
and CPUs per worker. Counts include newly created Jobs before their pods exist.
The snapshot uses this observation for the commissioning boards; missing or
stale worker evidence is unknown. Queue image admission reads this observation for the
ES queue, which Harvester does not serve. A held or stale controller does
not authorize image placement.

**Credential.** The pilots use the proxy the npps0 pilots use, the
production robot identity also used by the PanDA team's Harvester
(SysConfig `gke_pilots.proxy`, default
`/etc/swf-monitor/longproxy-for-rucio`). The cycle keeps it in the
namespace as the Secret `gke_pilots.secret_name`, and rewrites the
Secret when the file changes.

**Preemption.** When a spot node is reclaimed, its pod dies with its
job. The ES close-out, which lists this queue in `es_closeout.queues`,
credits the ranges the job finished within minutes
(NODE_EVENT_DISPATCHER.md, Preemption). The pilot passes finished
ranges to the server every 601 s, its built-in value, which overrides
the queue file's `zip_time_gap` (300 s there since 2026-10-01, as on
`BNL_NPPS_GPU`, with no effect on the cadence). The queue's `jobseed=all`,
requested from the PanDA team, regenerates the ranges it left.

## Settings (SysConfig `gke_pilots.*`)

| Key | Default | Meaning |
|---|---|---|
| `enabled` | False | the cycle starts pods only when true |
| `queue` | `BNL_ePIC_GOOGLE_es` | the PanDA queue served |
| `credential` | `/etc/swf-monitor/gke-sa.json` | the Google service-account key for the cluster (the PanDA team's Harvester key, `epic-harvester-sa-restricted`) |
| `cluster`, `location` | `epic-panda-us-east4`, `us-east4` | the GKE cluster; its endpoint and CA are read from the Kubernetes Engine API at each cycle |
| `namespace` | `default` | where the pods run |
| `template` | the package's `gke/pilot-job.yaml` | the pod template |
| `max_pods` | 0 | the most pilot pods alive at once |
| `cpu`, `memory` | `'14'`, `'100Gi'` | each pod's request and limit |
| `getjobrequests` | 5 | job requests before an idle pilot exits |
| `proxy`, `secret_name` | as above, `epicprod-pilot-proxy` | the credential |
| `native_runtime` | `.../canary/native-runtime-e8cc102` | immutable shared native launcher/archive bundle |
| `native_image` | `/cvmfs/singularity.opensciencegrid.org/eicweb/eic_xl:26.07.3-stable` | the image containing the pilot's Python and the payload |
| `es_channel` | `.../canary/es-channel-py313-debian13` | the channel matching Python 3.13 in that image |
| `queuedata` | `.../canary/queuedata/BNL_ePIC_GOOGLE_es.json` | existing ES storage/cadence overrides, preserved from the previous launcher |

The initial one-worker gate passed on 24 October 2026: task 41115/job 5893349
finished at 19:31:38 UTC, all 20 ranges `finished`, substatus `fg_done`, harness
and pilot errors zero. Four five-event units were reconstructed and merged into
one registered file. Independent JLab Rucio readback of
`epic:/TEST/canary/google-native-20261024T182959Z` confirmed 20 events,
7,439,041 bytes and Adler-32 `ad5db215`. The worker used shared runtime
`native-runtime-6d64079`, archive `9532f30b...27ae2`, campaign 26.07.1 and
Python 3.13.12. This proves native ES execution and registration; preemption
recovery remains to be validated.

After that proof, the configured cap was raised to the already authorized five
workers. Sustained commissioning tasks 41116–41120 each request 20,000 events,
250-event units, 300-second closes, automatic slots, a six-hour deadline with
a 30-minute drain margin, maxAttempt 10 and maxFailure 3. Their expiring output
datasets use `TEST/canary/google-native-sustained-20261024T193436Z-01` through
`-05`. Each pilot runs one ES job at a time; its 14 harness slots are within
that job, so the ceiling reserves at most 70 CPUs. The cap counts Jobs still
waiting for a pod and has no automatic increase. Expansion beyond five still
requires Torre's explicit approval.

The cycle refuses to start pods, and records why, while `enabled` is
false, `namespace` is empty, `max_pods` is 0, the credential is
missing or the template still carries a placeholder.

## The cluster, as read on 2026-10-01

The PanDA team gave the agent the key their Harvester uses, and the
cluster admits pandaserver02's address (192.153.161.16). A read-only
probe found:
- the node pool `epic-main-16-20gb`: n4-highmem-16 spot nodes,
  autoscaling to 35, each allocating 15.89 CPUs and about 116 GiB to
  pods;
- the namespace `default`, empty;
- CVMFS from the `cvmfs-nodeplugin` daemonset (namespace `cvmfs`),
  mounted on each node at `/var/lib/cvmfs-k8s`. A pilot pod mounts
  that path at `/cvmfs` with host-to-container propagation.

The service account cannot list the project's image registry. The
image is the grid image the Harvester pods ran (their pilot logs:
AlmaLinux 9.8, apptainer, uid 1000), `atlasadc/atlas-grid-almalinux9`.
The first pod confirms it. The image has no account for uid 1000, and
two things on the path need one: the Rucio client's `getpass.getuser()`
at stage-in (`getpwuid(): uid not found: 1000`, job 3809903) and
apptainer, which refuses to start the payload's containers ("unknown
userid 1000", job 3809911). The template's init container `account`
copies the image's `passwd` and `group` and appends the pilot's
account, `epicprod` (1000:1000, home `/pilotdir`), and the pilot
container mounts both over its own; `USER` and `LOGNAME` name it as
well. The node's default AppArmor profile refuses apptainer's mounts
in its user namespace ("Failed to set mount propagation: Permission
denied"), so the pilot container runs with AppArmor `Unconfined`; a
debug pod with that setting started the payload image as uid 1000
(2026-10-01).

## Still to do

- Observe sustained registered closes and preemption/range recovery under the
  authorized five-job cap. A controlled worker termination needs separate explicit authorization; no
  request to another person is authorized or pending.
- Cron enqueue (`*/5`) is installed and enabled; preserve the cap and shared
  native runtime while commissioning continues.
- Whether preemption notices reach the pod.
