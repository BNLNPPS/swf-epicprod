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

As built (2026-09-14): the cycle is `swf_epicprod/front.py`, run by the
production-operations agent's `front_cycle` doer (swf-monitor
`scripts/front-cycle.py`, timeout 240 s) every five minutes by cron
enqueue, in shadow mode: `front.mode` is `shadow` and `front.enabled`
is false at their seeded defaults, so every queue reads `held
(front_off)` until the switches are turned, and in shadow mode a feed
decision records `would_feed` and submits nothing; `shadow` is the only
mode the cycle accepts until the feed is built, and any other value is
recorded as an error and decided as shadow. The settings are the
SysConfig keys named above plus `front.queues`, `front.max_per_cycle`
and `front.activation_window_s`; the census is
`monitor_app/panda/census.py`, which also carries each queue's
non-terminal production tasks (the task cap's count) and p90 start
latency (the idle rule's clock); the decision records are the
`front_decision` and `front_cycle` actions, and the cycle stores its
whole state as the cached product `front_state`, which the front page
reads without computing. Every read the cycle makes is fenced: a
failed census, gate, backlog or decision is recorded for the queue it
concerns and the cycle continues; a gate that cannot be read reads
red. The gates in place: the passive canary verdict with the age of its
newest evidence window (failing, or older than 24 hours, is red), the
nightly credential check (not ok, or older than 36 hours, is red), the
breaker key, and the two fast detectors computed from the census's
six-hour window (burn-through: at least 10 failures, half of them fast,
none finished; failure window: at least 50 outcomes with 60% failed).
Declared downtime is an input since 2026-09-15 (the `declared` hold
below; the record and its other readers under Declared downtime). A
ready task's declared job count comes from its manifest, which reads
the input catalog, and is cached a day per task, per-job count and
matched inputs.

The feed (2026-09-16): `front.mode` takes `active`, and a `fed`
decision submits the task through `prodtask_submit_request`, the same
call the compose panel's Submit makes, with `front` as the editor and
`pcs_front_feed` as the attempt's association source (the manual path
records `pcs_submit_request`). The cycle still holds no credential: the
call allocates the attempt and enqueues the credentialed
`submit_evgen_task` doer, which records the jediTaskID back. A feed the
call refuses (the task already submitted, the agent queue unreachable)
is recorded as `error (feed_failed)` with the refusal and is not counted
as committed depth, so the next cycle decides again. A ready task with
an open attempt, an allocated `PandaTasks` row without a jediTaskID
that is not marked `submit_failed`, is ineligible with the problem
named on the page (a submission in flight, or an orphan the operator
records), so the front never submits a task twice. Phase two is the key
`front.jedi_throttled` (False): once the ePIC job throttler paces
generation in JEDI, the operator sets it true and committed depth
gains, per queue, the ungenerated rows of the queue's non-terminal
production tasks (`nFilesToBeUsed - nFilesUsed` over their input
datasets, carried by the census as `ungenerated`); the job cap and the
oversize hold then retire, as § Two regulators says, and the record
carries `phase`. The switches for commissioning one queue: `front.enabled`
true, `front.mode` active, `front.queue.<queue>.feed` true, `t_max` the
task cap; `max_per_cycle` and the activation window bound a cycle.

### States and reason codes

| State | Condition | Action | Reason |
|---|---|---|---|
| supplied | depth ≥ `h_low`, gates green | hold | `supplied` |
| refill | depth < `h_low`, gates green, feed on, no submission awaiting observation | submit the highest-priority eligible task; at most `max_per_cycle` per cycle, never past `h_high`, never past the caps | `fed:<task>` |
| awaiting observation | a submission is younger than one activation window (two cycles) | hold; count the submission as committed depth | `awaiting_observation` |
| idle capacity | running below a fraction of the ceiling while depth > 0, for longer than the queue's p90 start latency since the front's last feed | hold; notice (worker supply or site, not the front) | `not_pulling` |
| degraded | a gate is red: canary failing or its window stale, burn-through or windowed failure rate over threshold, credential invalid | hold; breaker opens | `canary`, `burn_through`, `failure_window`, `credential` |
| half open | the breaker's cause has cleared and policy allows automatic recovery | submit one task and wait for completions; on failure reopen with a doubled wait | `half_open` |
| held | queue feed off or front off; a declared downtime in force or starting within `h_high` hours (checked right after the breaker, ahead of the measured gates: a declaration is not a fault, the breaker stays closed and the hold lifts itself when the window ends); a feed that would pass `h_high`; the task or job cap; a task that cannot be sized; no calibration for the queue | hold | `queue_off`, `front_off`, `declared`, `would_exceed_high`, `task_cap`, `job_cap`, `unsized_task`, `no_calibration` |
| oversize | the next eligible task alone exceeds `h_high` (phase one) | hold for the operator's decision | `oversize_task` |
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
1 → 950, 2 → 900, 3 → 850, unset → 800; an operator's escalation is an
explicit value on the task's overrides (`task_priority`, e.g. 1000),
used verbatim (JEDI_INTEGRATION.md, the parameter table).
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
task pages; the readiness checks are what the dispatcher needs before
it places a task: a matched input, a sized configuration (a per-job
event count), an event target, a priority, and walltime and memory
within the pinned queue's limits (PCS_DATASET_REQUEST_WORKFLOW.md).
Two checks are recorded stamps rather than live reads, since readiness
renders on the compose page for every task and no remote call belongs
there: the input's replica availability, stamped on the match record
by the nightly EVGEN sweep, and a current payload-canary verdict for a
new or changed configuration, indexed by configuration when the
dispatcher's gate is built; readiness reads both. The commissioning
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

The engine's design and build plan are in
[EPIC_JOB_THROTTLER.md](EPIC_JOB_THROTTLER.md).

JEDI can regulate job generation per queue. The ATLAS engine
(`AtlasProdJobThrottler` over `JobThrottlerBase` in panda-server)
generates jobs for a work queue only while the queued jobs (activated
plus starting) stay under a multiple of the running jobs
(`THROTTLE_THRESHOLD`, 2.0 by default) and under a queue limit
(`NQUEUELIMIT`), and Harvester's `add_target_slots` raises that limit
to build job pressure for a fleet. The ePIC server does not yet run
one. The live `panda_jedi.cfg` registers `GenJobThrottler` for the
epic VO, which returns unthrottled whenever the work queue has no
share, and every epic work queue has none (verified 2026-09-14 in the
configuration, the `jedi_work_queue` table and the throttler's log). A
submitted task is therefore generated and activated in full within
minutes (`nFiles=5000` per 10-second generator cycle), and the
pressure front is today the only regulator of the activated pool at
each queue.

The target is two regulators in series. The front admits work from
`ready` in priority order and never lets a queue run dry; an ePIC job
throttler in JEDI paces generation per site against what the site is
running, so a task's jobs enter `activated` at the rate the queue
consumes them and a later high-priority task is not held behind a day
of activated work. The engine is derived from the ATLAS one, twenty
years of production experience, and tailored to ePIC: single-core
jobs, tasks pinned to one queue, the ATLAS rule and limits applied
per site rather than per work queue, so the epic work queues need no
share. It is built: `swf_epicprod/jedi/EpicProdJobThrottler.py`, with
its decision tested without a server (EPIC_JOB_THROTTLER.md § The
engine as built). Its registration for the epic VO, the package on
JEDI's path, the configuration rows and the generator's site
exclusion ride the pending server upgrade; it starts in `observe`
mode, logging its decisions while the server behaves as today, and is
switched to `throttle` by one configuration row once its readings have
been checked against the queue census. Its per-site limits are set
from the front's shadow-mode record.

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

Below the queue, the node: a black hole node kills every job it takes
within minutes inside a queue that is otherwise healthy, and the queue
breaker would stop the healthy queue to stop it. The node-level
instrument is the site canary's node guard (site-canary
docs/NODE_GUARD.md): every five minutes the window's terminal jobs per
queue and host are judged, a node whose jobs mostly fail, fail fast and
belong to tasks that finish on the queue's other nodes is a black hole,
a burst past a node count is read as the queue's event and handed here,
and the verdicts are on the Node guard page. It runs in shadow mode
first; its exclusion (the published list, the wrapper and landing
checks, the OSG clause) follows.

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
published and collectable, and the first source is the CRIC ePIC's
PanDA reads (datalake-cric.cern.ch, the DOMA instance named in
`/etc/panda/panda_server.cfg`). Three CRIC records carry declared
state, verified 2026-09-15:

- **PanDA queue status rules** (`api/atlas/pandaqueuestatus/query/?json`,
  `showall=1` for expired rules): per queue and activity, the value
  (`OFFLINE`, `BROKEROFF`, `TEST`), the expiration, the reason, who set
  it and when. This is the channel operators use and the one the pilot
  and JEDI obey: the BNL farm downtime of 2026-09-14 was four `OFFLINE`
  rules set by Xin at 02:18 UTC ("scheduled downtime") with expirations
  from 21:18 UTC to 00:21 UTC the next day. A rule has no start; it acts
  from the moment it is set.
- **DDM endpoint status rules** (`api/atlas/ddmendpointstatus/query/?json`):
  the same shape per storage endpoint and activity (read, write,
  delete). Storage downtime is declared here.
- **Downtime objects** (`api/core/downtime/query/?json`): a window with a
  start, an end, a severity, a classification, the affected services and
  an information URL, attached to a resource centre. Empty for EIC today
  and fed by nothing (BNL-OSG carries no GOCDB or OSG-topology link), but
  the record where a future window is declared ahead of time.

Reading any of the three needs an authenticated identity; the production
Rucio proxy the ops agent holds is accepted. The anonymous `pandaqueue`
query shows only a queue's effective status, no expiry or reason. The
pilot reads CRIC's cached queuedata, which lags a rule's expiration by
minutes: E1_BNL's rule expired at 00:21 UTC and a pilot at 00:24 UTC
still read it OFFLINE and aborted its job.

**The collector.** `cric_declared_state` on the ops agent
(`scripts/cric-declared-state.py`, ten-minutely by cron enqueue, directly
invokable) reads the three records with the proxy and keeps the
**declared record** in the entry store (swf-monitor
`monitor_app/declared.py`; kind `declared`, context `cric`): one entry per
rule or window, named by its source identity, with kind (queue, endpoint,
site), target name, value or severity, activity, start, end, reason, who
declared it and when, and a standing: `active` (in force now), `future`
(a window not yet begun), `expired` (its end passed) or `cleared` (gone
from CRIC before its end, with the instant it went). Every change is a
version of the entry and a `declared_state_sync` action naming what
appeared, expired or cleared, so the history is on the record without a
table of its own. The record holds the EIC queues, the endpoints those
queues name, and the resource centres behind them, nothing else.

**Where it shows.** The EIC queues list carries a Declared column beside
Status: the rule in force ("offline until 09/15 00:21 UTC: scheduled
downtime, Xin") or the next future window ("downtime 09/20 08:00 to
16:00 UTC"), blank when nothing is declared; the queue detail page has a
Declared state card with what is in force, what is coming, and the
recent history. The Rucio endpoints list carries the same column. No
calendar view: the lines and the cards are the surface, and the record
answers "was it declared at that time" for the readers below.

**Readers.** Built 2026-09-15: **attribution**, at read time through
the per-job error root (swf-monitor `panda/sql.py extract_errors`): a
faulty job whose queue was under a rule or a site window at its end, or
within ten minutes after the end (the pilot's cached queuedata lags a
rule's expiration), carries the declaration on every error entry; the
job page leads its Errors card with "Declared downtime: <line>", the job
lists prefix it, the MCP job tools carry it, and the error summary
counts the window's failures under each declaration (`declared` in the
summary; a line above the errors table). Nothing is written on the
job. **The front's gate** (`swf_epicprod/front.py`): a rule in force,
or a window starting within the queue's `h_high` hours, holds the
queue with reason `declared`, checked right after the breaker and
ahead of the measured gates; a hold, not `degraded`, so the breaker
never opens on a declaration and the hold lifts itself the cycle after
the window ends; the front page's queue table shows it. An unreadable
record reads as no declaration: the measured gates still stand.

Also built 2026-09-15: **the node guard** sets aside the jobs that
ended under a declaration (plus the lag) before judgment and counts
them on the cycle record and the Node guard page (site-canary
NODE_GUARD.md); **the canary** leaves a queue's status alone while a
rule is in force or its sample overlaps a declared span, sends no probe
to a queue under a rule in force, and sends the first probe after a
window's end at once, reading the record over `GET /api/declared/`
(site-canary SWF_INTEGRATION.md, Declared downtime); the EIC queues
list's Canary cell reads "declared" under a rule in force; **the
notice**: the sync emits one `declared_state_changed` incident per
rule or window that appeared, changed, expired or cleared (subject the
target, outcome the change, the line as `summary`, the target's page
as `url`, warning severity for an appearance), which a Capcom
subscription delivers (docs/NOTICE_ROUTING.md); **the home**: the
epicprod home carries one "Declared downtime" line when a queue or
endpoint has a rule in force or a window coming, nothing otherwise.

Later sources into the same record: the OSG Topology feed, the NERSC
status API and operator-entered windows. Endpoint rules are on the
record, the endpoint pages, the home line and the API; no reader
attributes a stage-out failure to one yet.

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
   engine is built (EPIC_JOB_THROTTLER.md); its registration for the
   epic VO, the configuration rows and the generator's site exclusion
   ride the pending server upgrade, in `observe` mode first, with the
   per-site limits set from the shadow-mode record; once it throttles,
   the front's phase-one caps retire.
9. Rucio exerciser and the Storage view.
10. Probe and rider build-out (site-canary increments 8–9), extending
    node-level evidence to every node work reaches.

## Asks and open items

- The EVGEN registration action run over the coverage worklist (the
  credential is in place; the action has not yet run for real).
- corePower for the GREX queue; harvester refill and ceiling; the
  pull-mode trial (PanDA operations).
- Later: a non-interactive service credential for the dispatcher.
