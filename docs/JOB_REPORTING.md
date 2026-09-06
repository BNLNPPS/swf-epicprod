# Job reporting

How a production job's own account of itself reaches the production
system, including from a job that fails.

The devcloud host is the gateway: it holds the object store this
depends on, manages it, and runs the watch over it. pandaserver02 holds
the record and runs the sweep that fills it. The division follows the
credentials: account operations belong where the account is, and the
production record belongs where the production database is.

## Summary

- Jobs write their report as objects to S3 directly. There is no ingest
  service and nothing to keep running.
- Every job writes, because which jobs will die is not knowable in
  advance. Only the ones that die are ever read.
- Objects live a week. A successful job's expire unread. A failed job's
  are swept for what is useful, filed in swfdb, and deleted; the sweep
  is not real time, and does not read every object when a storm's
  objects all say the same thing.
- Aggregate numbers keep coming from PanDA, which carries the node,
  site, CPU, memory, error codes and the payload's digest for every job
  whatever its outcome, and the full report for finished ones.
- Cost at the current job rate is about 26 dollars a month, set by
  writes. Reads and storage round to nothing. A defect that loops
  inside one job is the only unbounded risk.
- The production system watches its own usage and can stop the traffic
  on jobs already running, by disabling the write credential.

## Why

PanDA keeps job metadata for finished jobs only: in the thirty days to
2026-09-06, 365,477 failed ePIC jobs carried metadata not once, and the
log dataset is marked failed for every one of them. The payload's
digest rides the job metrics string, which does survive, but the
heartbeat is 1800 seconds and a job dying inside its first period says
nothing. The full account of a job that fails exists only in its
working directory and dies with the worker.

## Why object storage

Worker nodes cannot reach pandaserver02 and nothing can reach in to
them. What a site permits outbound is not negotiable from here: traffic
to a major cloud storage endpoint is ordinary and already established
at the sites ePIC runs on, and the same route already carries
production output from the GPU worker outside the facility enclave
(DEVCLOUD_STAGEOUT.md). A custom host is unknown to every site's policy
and can be blocked one site at a time.

Writing objects directly also removes a service to write, deploy,
secure and keep running, and removes rate limiting as a concern, since
the storage layer absorbs whatever a synchronized wave produces.

It gives up the reply: an object write cannot tell a payload to go
quiet. Load control is therefore what the job carries from submission,
plus the credential as the stop.

## The message

Each message is one object: kind, subject key, sequence, sent_at,
source, body. The first kind is the payload report; canary landing
reports and landing declines follow, which is why the envelope is
general rather than specific to the payload.

The payload writes at a small number of chosen boundaries under a hard
per-job cap, jittered, one attempt, short timeout, and open on failure.
A failed write is lost and never touches the job. The upload is signed
with the standard library alone (`payload/report_out.py`), since the
campaign container's contents are not ours to choose and the current
image carries no AWS library.

### As built

The object key is `reports/<PanDA job id>/<n>.json`, `n` counting the
job's writes from zero, so one job's reports list under one prefix and
the sweep reaches a job's objects without scanning. A job whose
environment carries no job id writes under `unidentified`.

The payload sends after each stage ends, at most twelve times, and
stops sending for the rest of the job after the first failed write: a
channel unreachable now stays unreachable, and retrying spends wall
time for nothing. Each write carries a jitter of up to a second, since
a wave of jobs crosses a stage boundary together. The cap is a constant
in `payload/run.sh`, not a setting, because a defect that writes in a
loop is the only unbounded cost this channel has.

The submit doer reads the bucket, region and key from a file held by
the operating account and writes them into the sandbox environment. The
task specification and the web tier carry no credential. A missing file
is not an error: jobs then run without this channel, which is how it is
turned off for new submissions.

## Storage, retention and the sweep

Reports share the devcloud stage-out bucket under their own prefix.
Objects expire after a week, which is slack for the sweep rather than a
retention period: a successful job's objects are redundant the moment
PanDA has its metadata and are never read, and a failed job's are worth
something only until what they say has been taken. The week absorbs an
outage of the sweep, or of the perimeter, without losing the reports
that matter, and a week of objects is a few gigabytes.

The drain exists to permit deletion. A sweep on the production
operations agent, on its own schedule and never in a request path,
reads the objects of failed jobs, files what is useful in swfdb beside
the job record, and deletes them. Expiry is the backstop for whatever
the sweep does not reach.

The sweep is selective. A storm produces thousands of objects that say
one thing: the same stage, the same reason, the same node or site.
Keeping a bounded number per distinct signature and deleting the rest
unread costs nothing in understanding and avoids reading a storm one
object at a time. The signature comes from the digest PanDA already
carries for every failed job, so the sweep knows what it is looking at
before it reads anything.

A job page may also fetch a single job's objects directly when someone
is looking at that job and the sweep has not reached it.

## Cost, and the stop

At 733,346 jobs in thirty days and a few writes each, writes are the
bill: roughly 26 dollars a month at current rates. Reads and storage
are negligible under the weekly expiry. Failure storms barely register:
the worst node of that month consumed 8,789 jobs in seven hours, about
thirty cents to report.

The unbounded risk is not the fleet, which is finite, but a defect that
posts in a loop within one job. Against it: a hard cap on messages per
job, enforced in the payload and not raisable by configuration, and a
size cap per message.

The watch belongs with the gateway. A cron on the devcloud host samples
the bucket's object count and growth rate, and alarms above a declared
ceiling. The stop is disabling the write credential, which reaches jobs
already running; a change to the job environment does not, since a
running job keeps the environment it started with. Every write then
fails, the payload treats a failed write as ordinary, and the traffic
drains as running jobs end.

## Credential

A write-only key, shipped into the job sandbox by the submit doer from
the production operations agent's environment, scoped to writing
objects under one prefix with no listing, reading or deletion. The task
specification and the web tier hold no credential.

The key sits on every worker node ePIC runs on and should be treated as
semi-public. What bounds a leak is the scope, the expiry, and the fact
that anything read back is validated against PanDA before it is
believed: a message naming a job that does not exist, or that is not an
ePIC production job, is discarded rather than filed.

## Open items

- Whether NERSC Perlmutter compute nodes reach object storage outbound.
  Perlmutter jobs register to the JLab catalog today, so an outbound
  path exists, but whether it is the compute node's own or is relayed
  is not established. If relayed, the route the pilot's heartbeats take
  is the candidate. A payload canary at that queue answers it.
- If Perlmutter cannot reach it, whether NERSC runs object storage of
  its own. A site-local endpoint is worse than one endpoint everywhere,
  since the sweep then has more than one place to look, but better than
  losing that site's reporting. The payload takes its endpoint from the
  job environment, so a per-site endpoint is configuration rather than
  code.
- PanDA's fine-grained processing is not expected to help: its
  granularity distributes work inward rather than carrying reports
  outward. Worth confirming before it is dismissed.

## Beyond this case

The same shape answers the GPU co-processor gateway and the Perlmutter
event service: a unit smaller than the job, travelling outward to a
store both sides can reach, collected on the system's own schedule,
with the loss of any single unit made cheap. Three tenants, one
gateway.

## Related

- [EPICPROD_PAYLOAD.md](EPICPROD_PAYLOAD.md) — the payload, its report,
  and what survives a failed job through PanDA.
- [DEVCLOUD_STAGEOUT.md](DEVCLOUD_STAGEOUT.md) — the bucket, its
  worker credential profile, and the stage-out path that established
  it.
- [NPPS0_WORKER.md](NPPS0_WORKER.md) — the perimeter-external worker
  this storage was first built for.
- [ERROR_ATTRIBUTION.md](https://github.com/BNLNPPS/swf-monitor/blob/main/docs/ERROR_ATTRIBUTION.md)
  — how a failed job is read once its evidence is in hand: which
  labels are unreliable, the grades of evidence that correct them, and
  why the failed-job record path is the exit fields rather than the
  metadata this channel exists to replace.
