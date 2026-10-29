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
in `ready`, together with the submissions people have queued for the
front (Placement, below); the pages call it the ready buffer.

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
loop (Placement, below). As of 2026-09-30 the front runs live:
`front.mode` is `active` and `front.enabled` is true.

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
  but has not started (activated, assigned, defined, sent), times the
  queue's median finished walltime, divided by the queue's running
  ceiling (the measured peak, or the declared capacity where one
  exists). A starting job counts as running: a job is recorded
  running late and works for most of its life as starting (on
  2026-10-05, the median finished job spent 87 of 115 minutes starting
  at BNL_OSG_EPIC_PROD_1 and 4.9 of 5.1 at UM_GREX_PanDA_1);
- **committed depth**: runnable depth plus every submission the front
  has made whose jobs are not yet visible (a task submitted and not yet
  generated, a submission request not yet answered), counted at its
  declared rows.

A queue's calibration (median and p90 walltime, p90 start latency) is
taken from its finished production jobs over 14 days, and counts only
from 20 of them (`CALIBRATION_MIN_JOBS` in `monitor_app/panda/census.py`).
Below that the queue reads uncalibrated: one fast trial job gave
UM_GREX_PanDA_1 a p90 start latency of 0.04 h.

Historical concurrency normalizes work depth. A running count below that
reference does not establish unused available slots and does not hold task
releases.

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
  queue; 0 is none. A paused task does not count: it generates no jobs
  until an operator resumes it (2026-10-21; three tasks paused since
  2026-08-17 had held BNL_OSG_EPIC_PROD_1 at its cap with nothing
  running). Off on every queue since 2026-10-21, and off by default: the
  job throttler bounds the activated pool and the high-water mark bounds
  what is committed to a queue, in hours rather than tasks.
- `nodes`, for a whole-node queue (Perlmutter and Google, one job per node),
  the nodes its work is to fill; 1 by default. It is set with the queue's
  worker ceiling, which is where the node count is enforced: the harvester's
  on Perlmutter (NERSC_PERLMUTTER.md, EVGEN input in production), the pod cap
  `gke_pilots.max_pods` on Google (GKE_PILOT_FLOW.md, EVGEN input).
- `feed`, the per-queue switch; `front.enabled`, the global switch.

`scripts/front-feed.py QUEUE on|off|exempt --by NAME --reason TEXT --decision-ref REF`
changes one feed switch atomically and records its previous and new values.
A feed is never off without a recorded reason (Torre Wenaus, 2026-10-28,
after GREX's feed was switched off with its reason in the audit record
alone and its dashboard bullet stayed green). The command writes the
reason, who and when where every display reads them:
`front.queue.<queue>.feed_off_reason` for `off`, `.feed_exempt` for
`exempt`, `.feed_set_by` and `.feed_set_at` for any change; `on` clears
both reasons. Run again with a new reason, it replaces the recorded one.
`exempt` is a queue whose work comes from its own feeder rather than the
front, Perlmutter's and Google's Event Service queues
(`swf_epicprod/perlmutter_production.py`, `google_production.py`): the
front feeds it nothing, as when off, but it is not a fault, and its
decision reads `queue_exempt`. The System page editor refuses a save
that leaves a regulated queue's feed off with neither reason. The skill
`site-controls` carries the commands.

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
SysConfig keys named above plus `front.max_per_cycle`
and `front.activation_window_s`, and the regulated queues are the
queue registry's (QUEUE_REGISTRY.md); the census is
`monitor_app/panda/census.py`, which also carries each queue's
non-terminal production tasks (the task cap's count) and p90 start
latency; the decision records are the
`front_decision` and `front_cycle` actions, and the cycle stores its
whole state as the cached product `front_state`, which the front page
reads without computing. Every read the cycle makes is fenced: a
failed census, gate, backlog or decision is recorded for the queue it
concerns and the cycle continues; a gate that cannot be read reads
red. The gates in place: the passive canary verdict with the age of its
newest evidence window (failing, or older than 24 hours, is red), the
nightly credential check (a credential expired, missing or unverifiable,
or the check older than 36 hours, is red; a credential still valid
inside its warning window is not, since its ping carries the date), the
breaker key, and the two fast detectors computed from the census's
six-hour window (burn-through: at least 10 failures, half of them fast,
none finished; failure window: at least 50 outcomes with 60% failed).
A failure of a task that a crash or memory finding explains
(SEGFAULT_DIAGNOSIS.md, Findings) is not counted against the queue it
ran on: the census counts it apart as explained, with the findings, and
the detectors read the rest. On 2026-10-20 the eAu memory kills (f-15)
held both OSG queues at 1,109 of 1,109 and 260 of 263 failed while their
canaries were healthy and twelve ready tasks waited for a queue. A queue
whose failures are all from such tasks is then judged by its canary.
An operator can also waive the fast detectors for a queue, for a stated
time and reason, in `front.gate_waivers` ({queue: {until, reason, by}},
`until` an ISO time); the gate then reads green with the waiver and the
reading it set aside in its reason, and the other gates stand.
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
`front.jedi_throttled` (False; true since 2026-10-21, the throttler
having throttled production since 2026-10-20): once the ePIC job
throttler paces generation in JEDI, the operator sets it true and
committed depth gains, per queue, the ungenerated rows of the queue's non-terminal
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
| degraded | a gate is red: canary failing or its window stale, burn-through or windowed failure rate over threshold, credential invalid | hold; breaker opens | `canary`, `burn_through`, `failure_window`, `credential` |
| half open | the breaker's cause has cleared and policy allows automatic recovery | submit one task and wait for completions; on failure reopen with a doubled wait | `half_open` |
| held | queue feed off or exempt, or front off; a declared downtime in force or starting within `h_high` hours (checked right after the breaker, ahead of the measured gates: a declaration is not a fault, the breaker stays closed and the hold lifts itself when the window ends); a feed that would pass `h_high`; the task or job cap; a task that cannot be sized; no calibration for the queue | hold | `queue_off`, `queue_exempt`, `front_off`, `declared`, `would_exceed_high`, `task_cap`, `job_cap`, `unsized_task`, `no_calibration` |
| oversize | the next eligible task alone exceeds `h_high` (phase one) | hold for the operator's decision | `oversize_task` |
| no work | nothing in `ready` is eligible for the queue | hold; the page states the starvation | `no_eligible_task` |
| node occupied | a whole-node queue (Perlmutter, Google: one job per node) whose own work fills its `nodes`: production jobs not started or running, plus files JEDI has not made jobs of, and at least one node for each live task | hold; below it, the next eligible task is fed whatever its hours, one a cycle, ahead of the calibration and depth checks, which measure hours (NERSC_PERLMUTTER.md, EVGEN input in production) | `node_occupied` |

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

### Placement

Production brokerage is never delegated to PanDA, and so never to CRIC.
Every submission names a production queue, which JEDI's brokerage takes
as pre-assigned. The production queues are the set the front
regulates, the queue registry's queues with the front flag
(QUEUE_REGISTRY.md). The front places work automatically; a person places and
moves it by hand through the Site control. Both use the same
recommendation and the same calls.

**Where a task goes.** `pcs.commands.pinned_site` reads, in order:
- the trial's `trial_site`;
- the task's own `panda_site` override, which placement sets;
- the configuration's `panda_site`.

A task with none of these is unplaced; the BNL_OSG_PanDA_1 default was
retired on 2026-09-30. Submitting an unplaced task places it first:
- from the page, on a queue a person selects in the Site control; left
  at the preselected recommendation, an unplaced task goes into the ready
  buffer unplaced and the front places it when it feeds it (from 26
  October: the page sent the preselected queue as a choice, which pinned
  tasks to GREX while OSG ran out of work);
- from REST or a rerun with no site given, on the recommendation;
- if no production queue is open to it, the submission is refused with
  the reason.

The submission kernel refuses a spec without a site.

A queue is eligible only when its pilot launchers can run the task's
exact container image. The queue registry reads that capability from
fresh template-derived harvester reports (QUEUE_REGISTRY.md, Pilot image
compatibility). A queue bound to another image is unplaceable, with the
required and served images in the reason. Manual placement, saved queue
choices and the final submission doer enforce the same check; unavailable
image evidence refuses submission rather than guessing.

**The front's placement.** Ready tasks hold no queue: they are agnostic
by definition (Torre Wenaus, 2026-10-28). Until then the front placed
every unplaced ready task on its best-ranked queue each cycle, before any
queue needed work, so all six ready tasks sat under OSG while GREX, its
feed just switched back on, decided `no_work` at 0 h of its 8 h low mark.
Now the unplaced tasks form one ready pool, and a queue takes a task from
it only when it decides to feed, below its low mark. The queues the front
feeds (feed switch on, and calibrated, since an uncalibrated queue holds
with `no_calibration`) decide in the recommendation's order with no
task's limits, the neediest first (`draw_order`), with this cycle's feed
switches. Each draws the pool's tasks open to it by the recommendation's
own reading (no red gate, a fit to its limits and image; `open_to`),
together with the tasks placed on it, in priority order; a task it
feeds leaves the pool, placed on that queue with the recommendation
recorded. A task too large for one queue's high mark stays in the pool
for the next. What no queue took stays unplaced and reads
`ready`, naming no queue, or says why it cannot go: not ready, or no
queue the front feeds (`left_in_pool`; Torre Wenaus, 2026-10-29). The ready hours size an
unplaced task at the median walltime of the queue first in line.

The ready buffer holds discretionary tasks for opportunistic assignment;
the one exception is a person's direction, a queue a person placed the
task on (Torre Wenaus, 2026-10-29). The front labels a task with a queue
only as it assigns it to that queue, and the label ends with that
assignment: when the task comes back to the ready buffer, a rerun or a
new submission with no queue named by a person, the front's queue is not
kept (`pcs.services.machine_placed`; every placement records who made it
in `placed_by`). On 2026-10-28 the front's assignment of p4495 and p4497
to Perlmutter came back into the buffer with their reruns as if pinned,
and held them for Perlmutter while OSG sat idle.

A queue under an operator core limit (`pilot.queue.<queue>.worker_limit`,
below) is sized at the limit: its ceiling is the smaller of its observed
peak and the limit, and its hours at capacity scale to match
(`core_limited`). GREX held to 296 cores was otherwise sized at the 949 it
once ran, so its 8 to 24 h marks committed three times the work the limit
can run.

Re-placing a task is a person's decision (Torre Wenaus, 2026-10-28:
"HUMANS GATE THE RE-PLACEMENT"): the Site control re-places a task not
yet in PanDA, and its Move re-places the unstarted work of one in PanDA
(below). The idle-queue move is the only automatic re-placement.

The Site control's recommendation applies the feed's whole-node rule
(`front.event_service_refusal`, read by `recommend` and `open_to` alike): a
whole-node queue is recommended only for Event Service work
(`front.whole_node_ready`). Until 29 October the rule sat in the feed alone, so
the Site control recommended Perlmutter for one-core particle-gun requests,
and p1108 130to177deg was pinned to a queue that would never take it.

**Reading and releasing a placement.** `pcs.services.prodtask_site_source(task)`
says where a task's queue comes from: `trial`, `task` (the
`panda_site` override placement wrote; an empty one sets aside the
configuration's queue, and reads `unplaced`), `configuration` or
`unplaced`. The Site control shows it, and the PanDA task's queue when
there is one.

**Unplace.** A placed task none of whose work has started is returned
to the front, which places it again by present conditions (Torre
Wenaus, 2026-10-28: "it should really be unplace; then the front
operates on it again based on present conditions"). It is a person's
decision, made with a reason that is recorded (`prodtask_unplace`,
posted live): the Site control's **Unplace** button, shown only when the
task can be unplaced, or the MCP tool `pcs_prodtask_unplace(task_name,
reason, by)` for a request made in dialog (skill `prodtask-placement`).
`pcs.services.unplace_blocker` says why a task cannot be unplaced: a
trial runs where it was fired, to qualify that path; an unplaced task
has nothing to unplace; a PanDA task in a status no move can take, or
with any job started or ended, keeps its started work where it is.
- Not yet in PanDA: the placement is removed (an empty `panda_site`
  override where the configuration names a queue) and the task waits in
  the ready pool for the next queue below its low mark. Its Event
  Service shape (`overrides['event_service']`) goes with the placement:
  a whole-node queue that takes the task sets it afresh at its feed,
  and any other queue submits ordinary jobs (Torre Wenaus, 2026-10-29,
  after p4495 and p4497, unplaced from Perlmutter, went to OSG as Event
  Service tasks and every job failed in the pilot).
- In PanDA with no job started: the task is marked
  `overrides['unplaced']` (who, when, why, the queue it was at) and joins
  the ready pool, sized at its declared jobs. The queue that takes it
  gets its unstarted work by PanDA's soft reassign
  (`prodtask_place_unplaced`, then `prodtask_move_request`, source
  `front_unplace`); when that is the queue it is at, it stays. Either
  way the mark clears, recorded `prodtask_unplace_placed`. Until a queue
  takes it, its jobs may still start where they are; then the mark
  lapses at the next cycle, recorded the same way, and started work
  stays.

Work in flight stays where it is: no Move and no job kills to change
its queue (Torre, 26 October 2026). A Move (`prodtask_move_request`) of
a task whose work has started is a task operation and needs Torre's
word.

Seen on 26 October: two p1089 tasks were pinned to GREX through the
compose page's preselection and were in PanDA by the time anyone looked
(41222, and 41324 with 499 of its 500 jobs done), so there was nothing
to release.

**A person's submission waits in line.** Submit, Rerun Residual and
Rerun Entire Task do not send the task to PanDA; they queue it for the
front (2026-10-05). The queued submission is
`ProdTask.overrides['queued']`: its kind (`submit`, `rerun_residual`,
`rerun_entire`), who queued it and when, the recommendation shown, and,
for a residual, the jobs it would run, measured when it is queued (the
JLab listing the preview reads). A queued task is in the front's ready
backlog whatever its status, at the queue it is placed on, or unplaced
for the front to place. The front submits it when the queue's committed
depth is below its low mark, as for any ready task, with the kind's own
submission (the residual is recomputed then). A queued task placed by a
person is fed even where the queue's feed switch is off: the person's
choice of queue is the consent, and the switch governs only the front's
own placements. Every other gate stands, and a task larger than the
queue's high mark is held as `oversize_task` with the reason on the
task's Site control. A queued task reads "in the ready buffer" on every
page: its Site control (with the front's latest reading of its queue
and the button that takes it out), the task's header, and the Queue
column and facet of the task lists, where the other tasks show the
queue of their PanDA submission or the queue placed on them
(`pcs.views.annotate_task_queue`).
"Submit now" (the Site control's checkbox) submits at once, as before;
it is the deliberate exception. GREX on 2026-09-30 is the case: three
residual reruns placed there by hand put 83,000 jobs into a queue that
runs about 20,000 a day, and jobs waiting past two days were reassigned
at the cost of an attempt each.

**The recommendation.** `swf_epicprod.front.recommend` is a pure
function over the front's stored state. Each cycle stores, per queue:
- the gates as judged (canary, credential, declared downtime, the fast
  detectors, the breaker), whatever the front's own switches decide;
- the queue's limits from schedconfig (`maxtime`, `maxrss`,
  `corecount`).

A queue is not recommendable when its feed switch is off, when a gate
is red, or when one job of the task exceeds a limit (walltime, memory
per job as memory per core times cores, cores). The rest rank in five
steps:
1. By canary standing, healthy first.
2. By recent failures: a queue whose gate window (six hours) holds at
   least 50 outcomes, 10% or more of them failed, ranks after the cleaner
   queues, well before its failure-window gate (60%) holds it.
3. By need, most first: the work the queue lacks to reach its low mark,
   in slot-hours (the low mark less its committed depth, times its slot
   ceiling), where idle slots come soonest. Until 26 October depth in
   hours alone ranked, and put 0.5 h on GREX's 949 slots ahead of 0.29 h
   on OSG's 4,425 while OSG ran out of work.
4. By committed depth in hours, least first. An uncalibrated queue with
   nothing queued counts as empty; one with jobs queued ranks after
   every known depth.
5. By p90 start latency, shortest first.

A queue whose feed switch is off is never recommended. A person may
still choose it explicitly, and work a person places there is fed;
`front.enabled` governs only the front's own feeding.

**Feed guard.** A feed switched off with an automatic return
(`front.queue.<queue>.feed_auto_on` = `exit78`, its reason in
`front.queue.<queue>.feed_off_reason`) comes back on at the start of the first
front cycle in which the queue has had no exit-78 failure for 90 minutes
and at least 100 finished jobs over the same window (`swf_epicprod/feed_guard.py`).
The return clears the reason, and with it the site's feed-off report
line, records `front` as who switched it and when, and is posted live
(`feed_auto_on`). GREX's feed was switched off this way on
2026-10-26 while its output copies to BNL failed.

**Closed queues.** A queue whose status in the queue registry
(QUEUE_REGISTRY.md) is `off` or `retired` is closed to production work
outright: the recommendation shows it with its reason and never ranks it,
and PCS refuses to place, submit, rerun, queue, trial or move work there
(`pcs.services.refuse_closed_queue`). Canary probes do not pass through
PCS submission and still run, except on a retired queue. Healthy gates do
not show capacity: on 2026-10-06 the recommendation named
NERSC_Perlmutter_epic, canary healthy and nothing queued, while the m3763
allocation was exhausted; both Perlmutter queues were closed "no
allocation" until the balance is read automatically, which now sets them
off and back.

A retired queue is excluded from the front, refused again by the
submission doer after resolving the final destination, and refused by
the site probes. Existing tasks keep generating jobs, retrying and
running: closing admission is not a task pause or pilot shutdown.
See [PROD_1_DRAIN.md](PROD_1_DRAIN.md) for the 22 October OSG drain.

**The NERSC balance.** The production-operations agent reads the
node-hours left in each NERSC project production charges
(`nersc.projects`, m3763) hourly from the NERSC IRI API
(`swf_epicprod/nersc_allocation.py`, doer `nersc_allocation_read`). The
access token is a file on this host that a PanDA operations cron
refreshes daily (`nersc.token_file`); the token's owner must be a member
of the project, since the IRI project list holds only the owner's
projects, and a project outside it reads "not visible to the token".
The balance is the cached product `nersc_allocation`, shown on the front
page beside the closed queues, and each read is a
`nersc_allocation_read` action.

Each readable balance opens and closes the NERSC queues: a queue in
`nersc.queues` (queue to project; both Perlmutter queues to m3763)
closes when its project has used `nersc.close_at_used` (0.9) or more of
its CPU node-hours, with a reason starting "NERSC allocation:", and
reopens when a new balance brings usage below it. A queue closed for
any other reason stays as the operator set it, and an unreadable balance
changes nothing. An explicit `commissioning` queue remains under operator
control for bounded validation; the balance neither closes it nor promotes it.
Each change is a `nersc_queue_switch` action.

**The Site control.** It sits on the compose page, in a task's detail,
and shows:
- the queue the task goes to and where that comes from;
- for a submitted task, where its PanDA task is and its state;
- the recommendation and its reason;
- the production queues, each with its queued work against its set
  points, jobs queued and running, p90 start latency, this task's hours
  on the queue at full capacity, its limits and the task's fit against
  them, and its gates.

The selected queue goes with Submit, Trial, Rerun Residual and Rerun
Entire Task. Move remaining work here, on the left, sends the current
PanDA task's work there. Place future tasks here, on the right, records
it on the task without submitting, so the task's later PanDA tasks go
there.

**Move.** Move is PanDA's task reassign in mode `soft`. It runs as the
`reassign` operation of swf-monitor `scripts/panda-task-operation.py`,
queued as a durable task operation for the production-operations agent.
What JEDI does:
- It kills the task's jobs that are not running, holding or
  transferring with code 51 (closed, `toreassign`). Harvester-fetched
  `starting` jobs are included.
- It sets the task's site and regenerates that work at the target.
- Running jobs finish in place.

Each killed job returns its file to ready with its `attemptNr` bumped.
A file on its last attempt would therefore be left exhausted, so the
operation follows a verified reassign with a one-attempt increase, which
raises `maxAttempt` on the task's ready files.

This was verified live on 2026-09-30 on trial task 40354, moved from
Perlmutter to UM_GREX_PanDA_1 with one queued job and a maximum of one
attempt. The job closed `toreassign` and left its file at attempt 1 of
1. After the increase, its work regenerated at GREX within seconds.

Move is offered while the PanDA task has work to generate or run
(defined, ready, running, scouting, scouted, pending, assigning,
throttled, paused). A finished, failed or exhausted task moves instead
by a retry with a target queue or a rerun with a site. A Move places
the task on the target too, so later submissions follow.

A paused task is resumed before it is moved, so a Move leaves it active
at the target. PanDA clears a paused task's previous state when a
command reaches it, so a task moved while paused cannot return to that
state: JEDI leaves it in `reassigning`. Only ATLAS's production watchdog
releases a task from there, and the ePIC VOs run the generic one. 40856
and 40857, moved while paused on 2026-10-20, sat there without jobs
until released by hand. Resumed first, the task is moved as a running
task is. The resume is verified before the Move is sent, and a resume
that does not take stops the Move.

**The stall signal.** The Site control marks a submitted task whose
waiting work is stuck where it is (`swf_epicprod.front.stall_signal`,
read from the PanDA task's unfinished and archived jobs). The bar is
the start latency of the queue the task waits at: its p90 (never below 1 h), or 6 h
when the queue has no calibration.
- **Stalled:** jobs are waiting, none is running, nothing has started
  for longer than the bar, and the oldest waiting job has waited past
  it.
- **Slow:** jobs are running, but the oldest waiting job has waited
  past the bar.

The signal states its numbers and points to Move.

**Bulk Move.** The PanDA task list's bulk actions include Move remaining
work, which sends every selected task to one production queue. It
carries the same mechanics and repayment as a single Move, paced and
verified on one clock. Like the other bulk actions, it is available on
the internal monitor.

**Idle-queue move (2026-10-26).** A task waits at the queue it was placed
on even when another production queue sits idle. On 2026-10-26 the ready
pool was empty and BNL_OSG_PanDA_1 had nothing running. Meanwhile nine
GREX tasks, 4,500 jobs, waited unstarted behind GREX's queue while GREX
started no new allocations. Torre moved them by hand and ruled that such
a move is automatic: the downside is nil, the upside large. Each front
cycle (`swf_epicprod/idle_move.py`, after the queue decisions) now moves a
task from its queue S to a queue T when all of these hold:
- T is idle: the front found nothing in `ready` for it (`no_work`), it is
  below its low mark, its feed switch is on and its canary is healthy;
- T is not failing: its failed jobs, those a finding explains included, are
  under 20% of its finished and failed jobs in the front's gate window, and a
  window with none does not establish it (Torre, 2026-10-26: "NO MOVE UNLESS
  FAILURE FRACTION BELOW 20%", after the rule moved task 41349's 500 jobs to
  GREX while 2,696 of its 7,343 jobs in the window had failed, under the
  front's 60% red). The same limit holds over the last hour when it holds 20
  finished and failed jobs or more, since a burst that resumes after a quiet
  spell is diluted in the six hours (GREX: 178 copy failures in the 20 minutes
  after its feed came back). The cycle record names each idle queue so
  refused, with both readings (`refused`);
- the task has never run: no job sent, starting, running, holding,
  transferring or merging, and none finished or failed, so started work
  stays where it runs and no pilot loses a job (Torre Wenaus,
  2026-10-29, after 41474, in production at GREX for days, was moved to
  OSG at a moment none of its jobs was running);
- the task is not paused (a resume is the operators'), not Event Service,
  not a trial, and its PCS task's current attempt;
- T is open to the task by the recommender's own reading: no red gate,
  no limit the task exceeds, and pilots that serve its exact image
  (QUEUE_REGISTRY.md, Pilot image compatibility; the Move rechecks it);
- the move leaves T no deeper than S: `T + task at T <= S - task at S`,
  in hours at each queue's capacity (The pressure measure). S may itself
  read `no_work`; the depths give the direction, so the moved work has
  no reason to come back;
- the task has had no reassign within the cooldown.

Tasks with no jobs generated go first, since moving one closes nothing;
then lower priority, then newer, which is the work that would start last
where it is. The move is the Site control's Move with the front as
requester (operation source `front_idle_move`): the soft reassign, the
attempt repayment, and the placement on T. Each move is an `idle_move`
action on epicprod-live with both depths before and after. The cycle
record and `front_state` carry the moves and the skips that matter (a
cooldown, an uncalibrated source, an idle queue the task cannot use).
Settings: `front.idle_move.mode` (`active`; `shadow` reports
`would_move`, `off` skips it), `.max_per_cycle` (4) and `.cooldown_h` (12).
It acts only when `front.enabled` is true and `front.mode` is active.

**The record.**
- Each placement is a `prodtask_place` action.
- An attempt records the queue it went to (`PandaTasks.site`) and, in
  `metadata.placement`, the recommended queue, its reason and whether
  it was followed.
- A Move's operation record carries the target, the previous site and
  the mode.

**REST.**
- GET `prod-tasks/<name>/site-options/`.
- POST `place/` and `move/`, each with `{site, placement}`.
- `submit/`, `rerun-residual/` and `rerun-entire-task/` take `site` and
  `placement`; `trial/` takes `site`.

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

## Pilot-side regulation: the harvester's queue limits

An operator worker-limit override is applied with
`scripts/worker-limit.py QUEUE N --cores --host HOST --by NAME --reason TEXT --apply`.
It changes Harvester's `maxWorkers`, preserves a dated configuration backup,
reads back the installed value, and records the active control in
`pilot.queue.<queue>.worker_limit`. The limit counts worker allocations;
a GREX allocation runs eight single-core pilots. Use `--cores` to specify
payload concurrency: 500 cores rounds down to 62 allocations, or 496 cores.
Use `--workers` for an allocation count. A positive limit requires one
of these explicit units. The control records both units and the dashboard
reports cores.
Existing allocations finish normally.
The override operates independently of the shadow pilot regulator.
Using `off` in place of `N` restores the saved prior limit and clears the
control. An outside change to the controlled value requires reconciliation
before the command will overwrite it. The active control is a numbered red
Production now line with its reason, who and when, cited at the site
panel's upper right (swf-monitor PROD_DASHBOARD.md); the reason says why,
not only the level (2026-10-28: "Torre: GREX limit lowered from 500 to 300
cores because it was still showing storage errors at 500").

The third regulator in series with the front and the job throttler. The
front admits tasks and the throttler paces their generation; neither
puts a pilot at the site. At
a push queue the harvester fetches activated jobs from PanDA and submits
one pilot per fetched job, within limits set per queue in its queue
configuration. Where those limits sit below what the site can run,
activated work waits for pilots, and a job that waits past PanDA's
two-day limit is reassigned at the cost of an attempt (the 2026-09-30
GREX case under Placement). The regulator sets the limits from the
front's census against a running target per queue. The harvester
itself is unchanged; the regulator edits only the values in the queue's
existing configuration entry.

**The limits.** UM_GREX_PanDA_1 is served by the harvester on
pandaharvester01 (version 0.7.5) in push mode (`mapType` `OneToOne`,
template `production.push`). Its entry in
`/opt/harvester/etc/panda/panda_queueconfig.json` is the live source:
the cacher carries no `queues_config_file`, so the harvester reads the
file, and re-reads it within its 600 s update interval without a
restart. Four groups of keys govern pilot supply:

| Key | Effect | Value on 2026-10-06 |
|---|---|---|
| `nQueueLimitJobMax`, `nQueueLimitJobMin`, `nQueueLimitJobRatio` | jobs fetched and not yet running are held at `ratio` percent of the running jobs, between min and max; the fetcher asks PanDA for more only below that | 2,000, 1,000, 100 |
| `nQueueLimitWorkerMax` | pilots submitted and not yet running at the site | 5,000 |
| `maxNewWorkersPerCycle` | new pilots per submitter cycle | 500 |
| `maxWorkers` | pilots queued, ready and running together | 5,000 |

A push-mode pilot is made only for a fetched job, so the fetch depth
also bounds the pilots queued at the site; the worker-ratio keys apply
to pull queues only and the harvester ignores them here.

**The measure.** Each front cycle reads, per queue: running pilots and
pilots queued at the site from the harvester's worker records, which
count a working job as running whatever its PanDA state (PanDA records
most of a GREX job's life as `starting`); pilots started and ended over
the interval and those that ended with errors; the activated jobs PanDA
holds for the queue; the queue's p90 start latency; and the limits in
force, read back from the harvester's `pq_table` by the harvester
reporter (swf-monitor `docs/HARVESTER_REPORTER.md`).

**The rule.** The running target `T` is set per queue by the operator
from the site's allocation (`pilot.queue.<queue>.target_running`; the
GREX allocation supports at least 1,000 running). The queued-pilot
target `Q` is the number of pilots the site starts in one hour at the
measured start rate, bounded below by a floor and above by `T`. The
limits follow:

- `maxWorkers` = `T` + `Q`;
- `nQueueLimitWorkerMax` = `Q`;
- `nQueueLimitJobMax` = `nQueueLimitJobMin` = `Q` plus one cycle's new
  pilots, with the ratio unset, so the jobs fetched and waiting track
  the queued pilots, not the running count;
- `maxNewWorkersPerCycle` = enough to refill `Q` within ten cycles.

A cycle raises a limit by at most half its current value and lowers it
at once. The cycle runs every five minutes, but a change is made only
after the reporter has read back the previous one, so at most once per
update interval. The limits are not raised, and the reason is recorded,
when:

- pilots stay queued at the site at `Q` for longer than an hour while
  running is below `T` (an hour fixed, since the census's start latency
  is the PanDA job's wait, which a backlog stretches to days): the
  site's scheduler is not
  starting them, and more supply would not help (`site_not_starting`,
  with a notice);
- PanDA holds fewer activated jobs for the queue than `Q`: supply
  follows work, which is the front's concern (`no_work`).

The limits drop to the floor when pilots end fast with errors above a
rate floor, or when a front gate for the queue is red (canary, breaker,
declared downtime), so that pilots do not consume jobs at a failing
site (`degraded`). When the limits in force differ from the last values
set, the entry was changed outside the regulator: it holds, records
both values (`changed_outside`) and notifies, and never overwrites the
outside change.

**The actuation.** A credentialed doer of the production-operations
agent, `harvester_queue_limits`, reaches pandaharvester01 over ssh,
reads the file, sets the keys above in the one queue's entry, validates
the JSON, writes a dated backup, and replaces the file atomically with
sudo. The action-stream event records the queue, the old and new
values, the measure and the reason. The cycle decides and enqueues the
doer and holds no credential. A change takes effect at the harvester's
next reload and is confirmed when the reporter reads the new limits
back.

**The record.** Each cycle writes a `pilot_decision` record per
regulated queue beside its `front_decision`, and the front page's queue
table shows the target, the queued and running pilots, the limits in
force and the values the regulator would set.

**Rollout.** `pilot.mode` `shadow` first: the cycle computes and
records the limits it would set and changes nothing, and the record is
compared with the realized running count. Then `active` on
UM_GREX_PanDA_1 alone (`pilot.queue.<queue>.enabled`), then the other
harvester queues one at a time. Once a queue is active, its entry is
set only by the regulator, with the `changed_outside` hold as the
guard.

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

### Task failure guard (23 October 2026)

On Torre's standing authorization after the 41039/41040 naming collisions,
each active front cycle checks production tasks on its regulated queues.
At **at least 50 completed job attempts and at least 95% failed**, it queues
a task pause through the existing production operations agent. The agent
verifies the PanDA state; running jobs finish normally. This guard is
independent of the queue's feed switch and gate waivers. The global front
switch and active/shadow mode apply; dry runs never enqueue operations.

The sample contains distinct `finished` and `failed` PandaIDs from the live
and archived job tables, excluding cancelled and closed records. It counts
attempts, including retried failures, to catch a retry storm. A verified
operator resume starts a new sample at its completion timestamp, so old
failures cannot immediately pause a repaired task again. Recovery remains
an operator action; this guard never resumes or kills work. Pending pauses
are deduplicated by the existing durable operation record. While another
operation on the task is in flight (a Move a person started), PanDA takes no
second one: the pause is deferred to the next cycle, recorded as `deferred`,
not as a front-cycle error (2026-10-27 20:43 ET, 41455 a minute into a Move;
paused at 20:54 once it finished). Read failures appear as front-cycle
errors, not as a healthy zero count.

The pause request records task identity, counts, sample boundary and threshold
in `PandaTaskOperation.evidence` and emits `task_failure_guard` on the live
action stream. The implementation is `swf_epicprod/task_failure_guard.py`.

**The rule mailed to the collaboration** (Torre Wenaus, 2026-10-23) runs
beside it, in shadow from 2026-10-24: pause when the latest 10 completed
attempts all failed, or 25 of the latest 50 did, counted in completion
order with each PandaID once. A failure whose worker ended under the job
(dispatcher 100, lost heartbeat; taskbuffer 300, worker finished while the
job ran) counts as neither failure nor success, and so does a job the pilot
cut at the slot's time limit (pilot 1213, reached maxtime; Torre Wenaus,
2026-10-24): the slot's clock, not the task's fault. A supervisor 1 rejection
whose diagnostic starts `got an update request with invalid batchID=` is
also set aside: the server rejected a worker identity, not the task's
payload. Other supervisor errors still count. The failures behind the
trigger must span at least three hosts, since a run on one node is the node
guard's. A verified resume starts a fresh sample, as above. Its mode is
SysConfig `front.task_guard.mode` (`shadow` by default, `active`, `off`);
its thresholds are `front.task_guard.run_failed`, `.window`,
`.window_failed` and `.min_hosts`. In shadow it records each trip once a day
as `task_failure_guard_shadow` and acts on nothing; active, it pauses through
the same agent path with source `front_task_failure_guard_rolling`. After
the shadow day only `front.task_guard.mode` changes: the first rule stays on
beside it (`front.task_guard.legacy` true; Torre Wenaus, 2026-10-24) as the
backstop for total failure where a queue reports few host names, as at
GREX.

Replayed over the recorded histories (2026-10-24), the rolling rule trips
41039, 41040, 40855, 40856 and 40857 within 2 to 6 minutes of their first
failure; the first rule trips four of them 2 to 5 minutes later and 41039 17
minutes earlier. 40431, at GREX, waits 277 minutes because GREX reports four
host names and no run of ten spanned three of them, while the first rule
paused it at once. On healthy tasks it tripped twice before time-limit kills
were set aside: 41051 on 2026-10-23 at 23:04Z, on a wave of 83 jobs cut by
the six-hour limit across 26 hosts, which no longer trips. The activation
review on 2026-10-25 also identified 40584's 2026-10-07 cascade as supervisor
1 invalid-batch-ID rejections. They had no pilot/dispatcher/taskbuffer error
code, but the supervisor diagnostic identifies a worker-identity fault;
they are now set aside rather than pausing the task. 41035, 41043, 41044
and 41052 never trip. The evidence records the observed failed count as
`window_failed` and the configured threshold separately as
`window_failed_threshold`.

### Breaker scopes

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
observation, no eligible task); it clears by itself and
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

The front feeds no task whose output storage is near full (2026-10-02:
BNL-XRD filled and thousands of finished jobs lost their output to it).
The swf-monitor alarm `storage_headroom` reads every output store each
tick, by the door's own space report, by the dCache quota, and by the
production account's quota in the catalog, and holds an event per store
below a tenth free. A ready task whose output RSE (`rucio_rse`, else
the payload's default EIC-XRD) has such an event, seen within the hour,
carries the readiness problem "its output storage is near full", so it
is ineligible for feeding and the ready page names why
(`full_output_rses` and `output_storage_problem` in
`swf_epicprod/front.py`). The hold lifts when the event clears. The
payload's stash at BNL-XRD is the backstop for a catalog outage, not
for a full store; preventing the full store is this hold and the alarm
ahead of it.

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
under-feeding, with pilot supply regulated beside it (§ Pilot-side
regulation); the site-side ceiling remains on the supply track.

## Supply side (parallel track)

The campaign-analysis measures stand: queue definition fields (maxtime,
corePower), the pull-mode trial, the JLab queue, the Google cap.
Harvester pilot supply is in the control loop (§ Pilot-side
regulation). None gate this build; each raises the ceiling the
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
9. Pilot-side regulation: shadow mode, then active on UM_GREX_PanDA_1,
   then the other harvester queues.
10. Rucio exerciser and the Storage view.
11. Probe and rider build-out (site-canary increments 8–9), extending
    node-level evidence to every node work reaches.

## Asks and open items

- The EVGEN registration action run over the coverage worklist (the
  credential is in place; the action has not yet run for real).
- corePower for the GREX queue; the pull-mode trial (PanDA operations).
- Later: a non-interactive service credential for the dispatcher.

## Regulation corrections, 22 October 2026

The [audit implementation record](REGULATION_AUDIT_2026-10-22.md) specifies
the coordinated clocks and OSG waiting buffer. The front requires fresh
JEDI enforcement evidence, excludes paused input from committed depth,
and counts the two OSG queues as one capacity pool with combined workload.
A 15-minute delivery grace replaces backlog-inflated historical start waits;
a deep buffer does not suppress the delivery warning. Oversize candidates
no longer block smaller eligible tasks behind them. Failure findings exempt
only their matching mechanism or exit code and payload stage.

Pilot decisions remain shadow-only. They use actual worker starts, convert
GREX's pilot target to eight-pilot worker allocations, and propose pull-mode
worker limits. Production notices independently evaluate supply health and
a sustained 25 percent running-count fall against the trailing hour while
work waits. Zero-running starvation has a five-minute grace. Google pilot
launching respects the front's explicit closure policy.

## Regulation evidence: alarm and reading, 29 October 2026

From 16:13 to 19:08 ET on 28 October the gate held every fed queue as
`regulator_unverified`: the throttler's decision lines gained fields
(`accounting=`, `scope=`) and the PanDA server reporter's fixed pattern
stopped matching. Nothing alarmed; Torre found it. Two changes follow.
The reporter (version 1.3) reads a `REGULATION_OK` line as key=value
fields in any order and counts, with a sample, a decision line it cannot
read (`jedi_regulation.unread`); the verdict names those lines when
regulation is unverified, so a changed format reads as one, though an
old unread line never holds a queue by itself. And the front records an
unverified episode held past `front.regulator_alarm_after_s` (600 s) as
one `front_regulator_unverified` action at severity alarm, again if its
reason changes, and its end as one recovery record: epicprod-live
publishes both, and the notify-llm subscription brings the alarm to the
AI sessions on swf-testbed (swf-monitor NOTICE_ROUTING.md).
