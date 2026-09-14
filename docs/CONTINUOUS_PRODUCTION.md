# Continuous Production — the ready queue, the dispatcher, and the tripwire

## Principle

epicprod production moves from passive retroactive recording of direct PanDA
task submissions to direct-control PCS-sourced
submission. Today submissions originate outside PCS and the system
learns of them after the fact, sweeping the record in with nightly
reconciliation. epicprod has no ability to sustain a pressure front of
ready tasks to continuously and automatically fill available resources.
The target state: PCS holds a deep queue of
submit-ready tasks, and a dispatcher keeps PanDA supplied with a
pending, priority-ordered workload that
drives continuous submission into as many resources as the
harvester/pilot infrastructure can acquire.
Crucial for this is strong validation of queue/storage/worker/payload
integrity before submitting in bulk, to avoid large scale drains due to errors:
a strong tripwire to protect the ready queue and the resources.

The pressure front and the tripwire are one system. Without the
protections, a standing queue amplifies the failure modes on record —
the 26.07 campaign spent 823K failed job attempts, and storm tasks ran
at 2.9 attempts per success.

## The ready queue

The ProdTask lifecycle `draft → ready → submitted → completed | failed`
exists (PCS_DATASET_REQUEST_WORKFLOW.md). The queue is every PCS ProdTask
in `ready`.

Campaign assembly fills it. For 26.09 the campaign specification can be
assembled as completely as the record allows — configurations, inputs,
event targets, priorities — with AI-generated system recommendations
("draft this task", "draft this task set") presented through the
AI-proposal mechanism (AI_PROPOSALS.md: the system proposes, a human
approves, execution is deterministic). Plan approval takes human approval
clicks and yields the deep task queue as a deterministic work queue.

Readiness gates, per task:

- input EVGEN dataset registered in JLab Rucio with available replicas;
- production configuration bound and sized;
- event target recorded (the completion denominator);
- validation checks passed;
- request priority mapped to `taskPriority`, so brokerage drains
  priority 1 first (submissions currently carry a uniform 900).

**Input-side automation is on the critical path: a ready queue starves
at the source while EVGEN registration is manual. The registration
action is built (`pcs/api/evgen/register/`, run by the ops agent) and
the agent holds the JLab credential it needs, door read plus Rucio
write. What remains is to run it over the registration coverage
worklist; it has not yet run for real.**

## Campaign assembly — the future plan

The campaign plan page is the assembly surface: a future-lifecycle
campaign's plan view is the proposal build. The row spine for a future
campaign is not edition heads — none exist before the software is
defined — but the physics configurations themselves: for 26.09, every
PC of the previous campaign. Each row carries a plan-membership
record — (PC, campaign, disposition, target events, priority,
provenance) — and the membership records are the plan. Software
definition turns the plan into editions and draft tasks by instancing,
copying target and priority onto the editions, so every existing
denominator reader is unchanged.

Each row arrives as a disposition proposal — include at prior size,
include at requested size, defer, or retire — pre-filled with its
evidence (the anchoring requests, delivered events and residual from
the completion record, the recorded priority) and with defaults in
the established target tiers (requested → prior-campaign delivered
snapped to round → derived). Target events and priority are editable
on the row before approval; the disposition is a flippable select;
each approval act carries one comment. Background conditions,
requestor curation, job sizing, and site targeting are deliberately
not plan-row fields — each has its own surface and its own time. Bulk
approval works the plan page's existing pattern: filter to a slice,
tick, approve as one act.

The AI proposal subsystem
([AI_PROPOSALS.md](https://github.com/BNLNPPS/swf-monitor/blob/main/docs/AI_PROPOSALS.md))
is the template, not a sketch — proposals as frozen executable
payloads, deterministic validate/decide/execute, origin-stamped
events, denial memory, the scan heartbeat, and the `.ai-attr` review
treatment all carry over, and the category follows the subsystem's
own checklist. The adaptations this category needs: **creation
subjects** (the proposal creates a plan-membership record, anchored on
an idempotency key — a growth path the subsystem reserves),
**edit-then-approve** (the reviewer may amend target and priority
before deciding; the amended values replace the proposed ones in the
payload and are recorded with the decision), and the plan page as the
category's domain review surface.

Contact coverage is a stated assembly objective, not a row field: each
anchoring request contributes its contact to the PC's registry (the
request composer already requires name and email), and the plan page
states coverage — contacts known for N of M configurations — so the
gaps are a worklist. Contact editing is PC-page curation.

## The dispatcher

The dispatcher is the front's regulator: a production-operations agent
loop that keeps every production queue supplied with work from `ready`,
in priority order, against a set point expressed in time, under the
tripwire's gate, with every decision on the action stream. The queue is
pinned per task at submission, so queue selection is entirely
production-side, with CRIC and PanDA configuration out of the control
loop.

### The pressure measure

Pressure is measured in time, not in tasks. Tasks differ by a factor of
twenty or more in job count (over the 26.07 production tasks, rows per
task run from about 350 at the tenth percentile to about 8,400 at the
ninetieth), while job walltime is nearly constant (median finished
walltime 1.4 to 2.2 hours at every production queue, every job
single-core). A count of pending tasks therefore says little about how
long a queue stays supplied, and a queue burning fast failures drains
any count quickly, which reads as consumption. The measure is the depth
of not-yet-running work at a queue in hours at the queue's capacity:

- **runnable depth**: the jobs of the queue's tasks that PanDA holds
  but has not started (activated, assigned, defined, starting, and the
  jobs Harvester has fetched for workers not yet running), times the
  queue's median finished walltime, divided by the queue's running
  ceiling (the measured peak, or the declared capacity where one
  exists);
- **committed depth**: runnable depth plus every submission the front
  has made whose jobs are not yet visible (a task submitted and not yet
  generated, a submission request not yet answered), counted at its
  declared rows.

The two are read together. Low runnable and low committed depth is a
queue that needs work; low runnable and high committed depth is work
held upstream of the queue (generation, brokerage, worker supply) that
more submission would not help. The useful completion rate of the
queue (finished jobs per hour over the last six hours) is a gate on
both: a queue with running jobs, no completions and failures above the
floor is consuming without producing, and its depth reads as infinite,
never as empty.

### Two regulators, two phases

Until the ePIC job throttler runs in JEDI (§ Queue-side regulation),
everything the front submits is activated at once, so the front alone
bounds the activated pool. In that phase the set points are on
runnable depth, the refill unit is the whole task, and two caps guard
the exposure per submission: an absolute job cap per queue and a task
cap per queue, and a task whose declared rows exceed the queue's
high-water mark is held for an operator's decision rather than
submitted. Once the throttler paces generation per queue, the activated
pool is JEDI's to bound: the front's set points move to committed
depth (enough work in JEDI for the throttler to draw on), the job cap
and the oversize hold retire, and the task cap remains as the
operator's bound on how much is committed where. Blocks, a task's
manifest submitted in parts as separate PanDA tasks writing to one open
output dataset, remain available as a further layer of protection and
are not part of the first build.

### Set points and caps

Per queue, in SysConfig under `front.queue.<queue>.*`, every edit an
action-stream event with its editor:

- `h_low`, the low-water mark in hours: below it the queue is refilled.
  The starting default is one shift, 8 hours at capacity, the longest
  gap the front is expected to be unattended.
- `h_high`, the high-water mark in hours: refilling stops at it. The
  starting default is one day at capacity; a deeper pool delays a later
  high-priority task by that much, since activated jobs are dispatched
  in priority order but frozen once Harvester has fetched them.
- `j_max`, the absolute job cap on the pool (phase one), defaulted from
  the measured peak running count until the Harvester fetch limits are
  reported.
- `t_max`, the maximum number of tasks with unstarted work pinned to the
  queue, a small integer.
- `feed`, the per-queue switch; `front.enabled`, the global switch.

In hours at capacity the queues differ mainly in scale: eight hours is
roughly 16,000 jobs at the OSG production queue at its burst capacity,
34,000 at Perlmutter, 3,800 at GREX. At sustained rates the same pools
last several times longer, which is the conservative direction for a
supply target. Set points are per queue; a share dimension is added
only when the global-share tree acquires leaves beyond the
production/analysis split.

### The loop and its record

The loop runs as a credential-free doer on the production-operations
agent's drumbeat, one cycle per five minutes aligned to the Snapper
census. It reads the per-queue job census (a service over
`jobsactive4` and the Harvester worker statistics), the canary verdicts
with their window bounds, the nightly credential check, declared
downtime once collected, the breaker states and switches, and the
ordered `ready` backlog with each task's declared rows and pinned
queue. It writes one decision record per queue per cycle to the action
stream (`subject_type='panda_queue'`), carrying the observation time,
the measured runnable and committed depth in jobs and hours, the set
points, each gate's value and age, the breaker state, the task chosen
if any, and a reason code. Feeds are always recorded; holds are
recorded when the reason changes and hourly as a heartbeat, so a
stalled front is one detection over the latest record. A feed is the
existing `/pcs/api/` submit action, which enqueues the credentialed
`submit_evgen_task` doer with its per-task dedup key; the loop holds no
credential and submits nothing itself.

The same records feed the ready-queue page: per queue its state, depth,
set points, gates, last feed and next candidate, and the ordered
`ready` backlog with per-queue eligibility. A later view shows the two
quantities as time bars per queue: hours of ready work on the source
side and hours of available capacity on the resource side.

### States and reason codes

| State | Condition | Action | Reason |
|---|---|---|---|
| supplied | depth ≥ `h_low`, gates green | hold | `supplied` |
| refill | depth < `h_low`, gates green, feed on, no submission awaiting observation | submit the highest-priority eligible task; at most two per cycle, never past `h_high` | `fed:<task>` |
| awaiting observation | a submission is younger than one activation window (two cycles) | hold; count the submission as committed depth | `awaiting_observation` |
| idle capacity | running below a fraction of the ceiling while depth > 0 for longer than the queue's p90 start latency | hold; notice (worker supply or site, not the front) | `not_pulling` |
| degraded | a gate is red: canary failing or its window stale, burn-through or windowed failure rate over threshold, declared downtime inside the horizon, credential invalid | hold; breaker opens | `canary`, `burn_through`, `failure_window`, `downtime`, `credential` |
| half open | the breaker's cause has cleared and policy allows automatic recovery | submit one task and wait for completions; on failure reopen with a doubled wait | `half_open` |
| held by operator | queue feed off or front off | hold | `queue_off`, `front_off` |
| oversize | the next eligible task exceeds `h_high` (phase one) | hold for the operator's decision | `oversize_task` |
| no work | nothing in `ready` is eligible for the queue | hold; the page states the starvation | `no_eligible_task` |

Three rules cover delayed observation. A submission counts as
committed depth for one activation window, so the loop never feeds
twice on the same gap. Refill starts below `h_low` and stops at
`h_high`, so census noise does not cause chatter. A breaker trips only
on a minimum number of terminal outcomes in its window.

### Priority

The request's priority (1 to 3) is copied to the task at creation and
carried by instancing; a task's own value overrides it, and a plan
entry's value (campaign assembly) overrides the request's. The live
task specification carries it as `taskPriority` under one mapping:
1 → 950, 2 → 900, 3 → 850, unset → 800, operator escalation → 1000.
Within a global share PanDA dispatches activated jobs by priority, so
the mapping orders the pool at every queue; `change_priority` reaches
activated jobs and is the lever for reordering what is already there,
exposed through the production-operations agent beside pause and
resume. No aging boost is applied; a starved low-priority task stays
where the operator put it.

### The intake

The dispatcher drains `ready` only. The manual Submit control remains
as an explicit operator path and records `origin=manual`; the
dispatcher records `origin=front`. Promotion to `ready` is a human
decision, one task or a filtered set at a time through the plan and
task pages; the readiness checks grow to what the dispatcher needs
before it places a task: the input dataset with an available replica,
a bound and sized configuration, an event target, a priority, walltime
and memory within the pinned queue's limits, and a current payload
canary verdict for a new or changed configuration. The commissioning
relaxation that lets a draft submit (COMMISSIONING_RELAXATIONS.md,
item 4) is retired once 26.09 intake has exercised the draft → ready
path in volume.

Credentials: the loop runs under the operator credential exactly as
submissions run today. Lifetimes measured by the nightly credential
check: OIDC token 274 days, JLab output proxy 59 days, BNL Rucio proxy
9 days. Required additions: alarms on the expiries (the check exists;
alarm surfacing is pending) and a renewal drumbeat. A non-interactive
service credential is a later robustness improvement
(JEDI_INTEGRATION.md follow-up 2), not a prerequisite.

## Queue-side regulation: the ePIC job throttler

JEDI can regulate job generation per queue. The ATLAS engine
(`AtlasProdJobThrottler` over `JobThrottlerBase` in panda-server)
generates jobs for a work queue only while the queued jobs (activated
plus starting) stay under a multiple of the running jobs
(`THROTTLE_THRESHOLD`, 2.0 by default) and under a queue limit
(`NQUEUELIMIT`), and Harvester's `add_target_slots` raises that limit
to build job pressure for a fleet. ePIC does not run it. The live
`panda_jedi.cfg` registers `GenJobThrottler` for the epic VO, which
returns unthrottled whenever the work queue has no share, and every
epic work queue has none (verified 2026-09-14 in the configuration,
the `jedi_work_queue` table and the throttler's log). A submitted task
is therefore generated and activated in full within minutes
(`nFiles=5000` per 10-second generator cycle), and the pressure front
is today the only regulator of the activated pool at each queue.

The target is two regulators in series. The front admits work from
`ready` in priority order and never lets a queue run dry; an ePIC job
throttler in JEDI paces generation per queue against what the queue
is running, so a task's jobs enter `activated` at the rate the queue
consumes them and a later high-priority task is not held behind a day
of activated work. The engine is derived from the ATLAS one, twenty
years of production experience, and tailored to ePIC: single-core
jobs, tasks pinned to one queue, the epic work queues given shares,
`NQUEUELIMIT` and `THROTTLE_THRESHOLD` set per queue from the measured
record, corePower honest at every queue, and the front's canary and
breaker state respected. We write the engine; registering it for the
epic VO on the PanDA server and setting the work-queue shares are the
PanDA team's actions. It is built as a parallel track while the front
commissions: the front runs guarded first, the engine's per-queue
limits are set from the front's shadow-mode record, and its inputs
(shares, corePower, honest job metrics) are the ones native scouts
need as well.

## The submission ladder

1. **Canary probe** — site integrity before production. A small
   dedicated job built from the real payload (site-canary increment 8),
   exercising the full path including stage-out to the output RSE. A
   queue with no current green canary receives no production tasks.
   Probes run on the adaptive cadence: sparse when healthy, dense when
   the platform alarm indicates trouble. The probe payload and the
   representative test job requested for resource estimation are the
   same artifact.
2. **Payload validation (mini-scout)** — task integrity and sizing
   before bulk commitment, staged in two forms. The first stage is the
   payload canary (site-canary IMPLEMENTATION.md § Payload canaries):
   the production payload on one manifest row of the configuration, as
   a canary task with an expiring output dataset and a checklist
   verdict on the canary page, run per new or changed configuration
   and gated by the dispatcher; it has no prerequisites and works
   today. The target mechanism is JEDI's
   native scouts, used lightly: JEDI runs a few scout jobs per task,
   measures cpuTime, ramCount, output and scratch size, and I/O
   intensity, adjusts the task parameters, and avalanches only when
   scouts succeed; scout failure parks the task `exhausted` instead of
   draining a site. Native scouts are preferred once they are honest,
   because the avalanche gate then lives inside PanDA — it cannot be
   bypassed by any submission path — and task sizing comes free.
   Honest scouts require payload event and CPU reporting (below),
   per-queue corePower set (zero at one site today), and resolution of
   the noInput/HS06 walltime pitfall that motivated the current
   skip_scout default. In either form the check runs for new or
   changed configurations and stays off for cloned, proven ones, so
   the added latency is paid only where it buys protection.
3. **Avalanche**, under the standing tripwire.

## The tripwire

Drains have three shapes, so the breaker has three scopes:

- **queue breaker** — a site eating jobs (memory storms, site
  configuration faults): stop feeding the queue; optionally pause the
  tasks pinned there.
- **task breaker** — a bad payload draining everywhere (a segfaulting
  configuration, jobs that cannot fit a slot): pause the task (a
  verified PanDA operation), not the queue.
- **global breaker** — an infrastructure outage (catalog, storage):
  stop the front.

Detection comes from the canary short-window verdicts (incident
windows, failure attribution, the fast-failure burn-through signature —
the passive-assessment gate list in the site-canary plan) and from the
five-minute per-job errors record. Notification is Snapper and the
alarm engine; the engine records and notifies, holds no credentials,
and never actuates. Actuation belongs to the dispatcher and the
production-operations agent alone, restricted to defensive, reversible
moves — stop feeding, pause — never kill. Every actuation is an
action-stream event and an entry on the production notice stream, the
distilled feed relayed to Mattermost — every tripwire firing is
logged there.
Recovery is via operator, or automatic when the verdict clears and policy
allows.

Two kinds of stop are kept apart in the record and on the page. A hold
is the dispatcher's own transient decision (supplied, awaiting
observation, idle capacity, no eligible task); it clears by itself and
is never latched. A breaker is a latched state on a queue, a task or
the front, opened by a detection or an operator and closed by an
operator or, per breaker class and only where policy allows, after one
half-open task completes. Opening a breaker changes no ProdTask state:
`ready` tasks stay `ready` in their order; the only PanDA-side action
is pause of the tasks already submitted, reversible within one
TaskCommando cycle and touching no running job. Closing a breaker
resumes feeding through half open, never directly to full refill. A
task breaker pauses the task and withholds it at every queue; a queue
breaker withholds every task from the queue; the global breaker stops
the front and takes precedence.

Until the alarm-queue detection modules exist, the dispatcher's own
gate computes the two fast conditions from the job record: burn-through
(failures ending in under a quarter of the queue's median finished
walltime, above a rate floor, within a one-to-two-hour window) and the
windowed failure rate (for storms whose failures run the full job
length). The front never runs without a fast detector.

## Storage health

Stage-out is part of every canary probe. In addition, a standalone
Rucio exerciser joins the production-operations drumbeat: a cycle of
upload, register, replica-check, read-back, and delete against each
production RSE on a cadence, published as a storage Snapper component
feeding a new Storage view — storage faults surface before payloads
find them. The single catalog instance for science data is on the
record as a resilience item.

## Declared downtime

Measured health is not the only evidence; planned downtime is
published and collectable. A downtime collector joins the
production-operations drumbeat: the OSG Topology downtime feed for
grid resources, the NERSC status API for Perlmutter, and
operator-entered windows for sites publishing no feed. Declared
windows surface on the EIC queues page as the next planned
maintenance per queue, and enter the dispatcher's gate: a queue
entering a declared window within the submission horizon is not fed,
and its canary cadence tightens at window end to confirm recovery.

## Payload metrics

The payload is production's own and reports for itself
([EPICPROD_PAYLOAD.md](EPICPROD_PAYLOAD.md)): every stage runs under
prmon, and the per-stage CPU, memory and event counts are written to
`payload-report.json` and carried into `jobReport.json`, which the
pilot ships as job metadata; a compact digest rides the job metrics
string. PanDA keeps metadata for finished jobs only, so a job that
dies reports out of band ([JOB_REPORTING.md](JOB_REPORTING.md)).
Scouts, job sizing, honest efficiency, and drain detection all depend
on this reporting.

## Demand-side evidence

Fed queues sit idle for lack of work under the current mode. For example the GREX
site contact states about six times the current use is available under
fair share; the queue has run at exactly its 950-job ceiling on some
days and near idle on many others, and carries about a hundred running
jobs at this writing. A standing pressure front is the fix for chronic
under-feeding; the site-side ceiling and harvester items remain on the
supply track.

## Supply side (parallel track)

The campaign-analysis measures stand: queue definition fields (maxtime,
corePower), harvester slot refill, the pull-mode trial, the JLab queue,
the Google cap. None gate this build; each raises the ceiling the
pressure front can reach.

## Sequencing

1. 26.09 assembly on PCS intake: required event counts and priorities;
   targets set at assembly; system-recommended task drafts approved
   through the proposal surface; priority→taskPriority mapping.
2. EVGEN registration run over the coverage worklist; inputs registered
   ahead of need.
3. Dispatcher prerequisites: `taskPriority` in the live task
   specification under the mapping; the per-queue job census as a
   service; the readiness checks widened; the dispatcher's intake
   restricted to `ready`.
4. Dispatcher in shadow mode: the loop computes depth, gates and reason
   codes per queue, writes the decision records and the ready-queue
   page, and submits nothing; its predictions are compared with the
   realized drain.
5. Dispatcher commissioning: one queue enabled, one task per fresh
   observation, the caps and switches in SysConfig, submission through
   the existing action; then the remaining queues.
6. Tripwire v1: the fast detectors in the dispatcher's gate; queue and
   task breakers with pause, notices and the operator recovery surface;
   credential expiries alarmed.
7. Native scouts replace the canary payload gate for new
   configurations, once payload reporting and corePower are in place.
8. The ePIC job throttler in JEDI, a parallel track from item 4: the
   engine derived from `AtlasProdJobThrottler` and tailored to ePIC is
   designed and coded while the front commissions, its per-queue limits
   set from the shadow-mode record; registration for the epic VO and
   the work-queue shares follow, and the front's phase-one caps retire.
9. Rucio exerciser and the Storage view.
10. Probe and rider build-out (site-canary increments 8–9), extending
    node-level evidence to every node work reaches.

## Asks and open items

- The EVGEN registration action run over the coverage worklist (the
  credential is in place; the action has not yet run for real).
- corePower for the GREX queue; harvester refill and ceiling; the
  pull-mode trial (PanDA operations).
- When the ePIC job throttler is ready: its registration for the epic
  VO in `panda_jedi.cfg` and shares on the epic work queues (PanDA
  operations).
- Later: a non-interactive service credential for the dispatcher.
