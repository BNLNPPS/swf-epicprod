# Pilot flow on Google Cloud: BNL_ePIC_GOOGLE_es

The production operations agent manages the pilot flow of the Event
Service queue `BNL_ePIC_GOOGLE_es` itself: it starts the pilot pods in
the GKE cluster, sizes each to a whole node, and keeps their number in
step with the queue's Event Service backlog. PanDA's Harvester does not
serve this queue. `BNL_ePIC_GOOGLE` stays with the PanDA team's
Harvester (`BNL_harvester_2`).

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
   cluster, by label, and counts their pods as waiting (Pending) or
   working (Running).
3. **Decides** (`decide`, a pure function). The pods to start are the
   activated jobs not already covered by a waiting pod, bounded by the
   cap `max_pods` less every pod alive. With no backlog nothing starts.
4. **Starts them.** Each is one Kubernetes Job, one pod, from the pod
   template, and runs our pilot wrapper in pull mode.
5. **Records** a `gke_pilot_cycle` action when it starts pods or its
   reason changes.

Finished Jobs remove themselves (`ttlSecondsAfterFinished`). A pilot
that finds no job exits after its `--getjobrequests` attempts.

## The pod

The template is `swf_epicprod/gke/pilot-job.yaml`. The site-specific
parts are in it: the image, the CVMFS volumes and the node pool. They
are to be filled in from the cluster once the agent has its credential.
The cycle sets:
- the name and labels;
- the CPU and memory, the `cpu` and `memory` settings, sized so that
  one pod takes one node;
- the proxy, mounted from the Secret the cycle keeps current;
- the command: our wrapper,
  `/cvmfs/eic.opensciencegrid.org/panda/pilot/canary/epicprod-pilot-wrapper.sh`,
  with `-q -r -s <queue> -i PR -j managed --getjobrequests N` and no
  resource type.

The wrapper sets `ATHENA_PROC_NUMBER` from the pod's CPU limit and
runs our pilot build (OSG_SUBMISSION.md, Our pilot wrapper). The
payload's harness takes its slots from the same number
(NODE_EVENT_DISPATCHER.md, The core count, from the node).

**Credential.** The pilots use the proxy the npps0 pilots use, the
production robot identity also used by the PanDA team's Harvester
(SysConfig `gke_pilots.proxy`, default
`/etc/swf-monitor/longproxy-for-rucio`). The cycle keeps it in the
namespace as the Secret `gke_pilots.secret_name`, and rewrites the
Secret when the file changes.

**Preemption.** When a spot node is reclaimed, its pod dies with its
job. The ES close-out, which lists this queue in `es_closeout.queues`,
credits the ranges the job finished within minutes
(NODE_EVENT_DISPATCHER.md, Preemption). The queue's `jobseed=all`,
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
The first pod confirms it.

## Still to do

- A first pod by hand (`max_pods` 1) against an Event Service trial
  task with `--es-loop`, then the cap raised.
- Cron enqueue (`*/5`) with the first enabled cycle.
- Whether preemption notices reach the pod.
