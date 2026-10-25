# Worker-state uncertainty and dispatch safety

The reviewed repair was installed on Torre's direct instruction, "INSTALL IT",
on 25 October 2026. Harvester was active at 17:20:54 UTC and PanDA HTTP at
17:21:32 UTC. The preparation tools themselves only write copies, diffs and
SHA-256 manifests outside their input package.

## Installation and rollback record

The installed content is source commit e7d5eb9. Exact input hashes were checked
before any writes; output hashes match the independent review. Dated copies of
each original file remain beside that file, ending
`.before-worker-safety-20261025T172038Z` for Harvester and
`.before-worker-safety-20261025T172111Z` for PanDA.

- Harvester audit: osgsub01,
  /opt/harvester/worker-safety-20261025T172038Z/installation.json.
  Activated through the documented panda_harvester-uwsgi service.
- PanDA audit: pandaserver01,
  /opt/panda/worker-safety-20261025T172111Z/installation.json.
  Only panda_httpd was restarted; JEDI and the daemon were not restarted.
- The API health endpoint returned 200. Fresh Harvester monitor and HTCondor
  cycles appeared without tracebacks in the sampled post-activation log tails.
  At 17:23:24 UTC the central join contained 52 running, two starting and one
  sent OSG job started since HTTP activation, all with running parent records.
  This establishes continuing valid dispatch, not complete incident recovery.
- No worker/job state resurrection or worker removal accompanied this repair.

To roll back, restore these exact dated source copies, preserve their modes and
owners, and restart only the affected service above. Keep the failed patched
copies and audit records for diagnosis. Neither rollback nor an upstream package
upgrade should silently discard the other installed compatibility patches.

## Incident evidence

On 25 October 2026, OSG worker records marked cancelled subsequently acquired
new PanDA jobs. A central database join at 15:39:37 UTC found 348 such active
relations, including 346 running jobs. Both worker and job identifiers are
needed: a Condor running state alone does not establish job acquisition.

The installed Harvester was 0.7.5, with checkTimeout 3,600 seconds and
cancelUnknown false. A retained monitor record for worker 2415575 at
13:01:59 UTC explicitly reports cancellation after consecutive failed checks
and a missing Condor job. At 11:58:54 UTC a failed schedd connection and a
missing-worker observation appear at the same timestamp in the HTCondor monitor
log. These witnesses establish the query-failure-to-cancellation path in this
outage. They do not prove the cancellation cause of every
affected worker or identify why the scheduler query missed each request.

Harvester's HTCondor monitor converts query exceptions to an empty result and
reports a successful check. The monitor body eventually converts repeated
unknown results to cancellation. Separately, PanDA getJobs calls updateWorkers
after selecting a job, while updateWorkers preserves recent terminal worker
states. A new job can therefore be attached to a parent that stays cancelled.

The original records, deployed-source captures, query and CSV are retained on
swf-testbed under /data/wenauseic/swf-delivery/osg-worker-state-audit-20261025.
The independent causal trace is under
/data/wenauseic/swf-incident-archives/osgsub01-20261025. The worker HTTP 403
startup failure and its carried-queuedata repair are a separate incident path.

## Prepared behavior

The Harvester patch distinguishes a failed query from an empty successful
answer. A failed plugin query leaves the batch's worker states unchanged and
reports the failure. A failed command-line query raises instead of manufacturing
missing workers. After a successful query, an HTCondor worker with unknown native
state is retained past checkTimeout, with a WORKER_STATE_UNVERIFIED error.
An explicit terminal Condor record still receives its existing treatment.
Other monitor plugins retain their timeout behavior.

Unknown is unresolved, not proof of life. Retaining unknown workers can retain
capacity accounting for a genuinely disappeared allocation. Such workers require
reconciliation using scheduler terminal records and pilot/job evidence. This
patch does not invent an expiry, revive terminal parents or remove live payloads.
Existing explicit worker kill and configured heartbeat policies are unchanged.

The separate PanDA patch checks the supplied parent in the existing dispatch
transaction, before changing a candidate job to sent. It locks an existing worker
row with SELECT FOR UPDATE. A finished, failed, cancelled or missed parent causes
rollback and refusal of new work. Previously returned jobs remain in the response.
Missing-parent registration and non-Harvester requests retain their semantics.
Job status updates and final output registration are not changed.

## Preparation and verification

Run from the repository with the standard SWF environment active:

```bash
python scripts/prepare-harvester-monitor-safety.py \
  --package /path/to/captured/pandaharvester \
  --output /path/to/prepared-harvester
python scripts/prepare-panda-worker-dispatch-guard.py \
  --source /path/to/captured/job_complex_module.py \
  --output /path/to/prepared-panda
HARVESTER_SAFETY_SOURCE=/path/to/captured/pandaharvester \
  PANDA_SAFETY_SOURCE=/path/to/captured/job_complex_module.py \
  python -m unittest discover -s tests -p test_worker_monitor_safety.py -v
```

The regression checks execute the captured Harvester wrapper and monitor core,
including the FIFO retry contract, and the captured PanDA getJobs body, with
scheduler and database boundaries mocked. They cover failed queries, unknown
states past the timeout, explicit batch removal, other plugins' policy and
terminal-parent dispatch before candidate mutation. They do not prove distributed
behavior or production recovery. The exact installed source must match the reviewed input hashes before
an installation is considered; unexpected patch contexts are refused.

swf-2 owns implementation and focused verification; swf-3 owns the causal trace
and independent review. swf-1 owns the separate native startup/refill repair.
Any production installation needs its own scoped maintenance decision and
validation, including successful job completion on valid worker parents. The
PanDA service maintainer owns changes to pandaserver01.
