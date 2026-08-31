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
action is designed and awaits the JLab credential (door read plus
Rucio write) — a standing ask.**

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

The dispatcher is a production-operations agent
loop that

- keeps a target number of tasks pending per queue and share, refilled
  as PanDA drains them;
- drains `ready` in priority order;
- targets queues directly — the queue is pinned per task at submission,
  so queue selection is entirely production-side, with CRIC and PanDA
  configuration out of the control loop;
- runs the submission ladder per queue (below);
- consults health verdicts before every cycle — the tripwire's gate;
- records every action in the action stream.

Credentials: the loop runs under the operator credential exactly as
submissions run today. Lifetimes measured by the nightly credential
check: OIDC token 274 days, JLab output proxy 59 days, BNL Rucio proxy
9 days. Required additions: alarms on the expiries (the check exists;
alarm surfacing is pending) and a renewal drumbeat. A non-interactive
service credential is a later robustness improvement
(JEDI_INTEGRATION.md follow-up 2), not a prerequisite.

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
   before bulk commitment, staged in two forms. The first stage is a
   canary payload task of one or two jobs per new or changed
   configuration, submitted and gated by the dispatcher; it has no
   prerequisites and works today. The target mechanism is JEDI's
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

The metatable self-report channel exists and is empty. Once tasks and
jobs are PCS-submitted, the payload is production's own: event and CPU
reporting from the payload is implemented by production, coordinated
with the payload owners. Scouts, job sizing, honest efficiency, and
drain detection all depend on it.

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
2. EVGEN registration action live (JLab credential); inputs registered
   ahead of need.
3. Readiness checks promote draft → ready; the queue fills.
4. Dispatcher v1: keep-N pending, priority-ordered, site-canary-gated,
   with the canary payload gate on new or changed configurations and
   credential expiries alarmed.
5. Tripwire v1: queue and task breakers wired from existing detections,
   with notices and the operator recovery surface.
6. Native scouts replace the canary payload gate for new
   configurations, once payload reporting and corePower are in place.
7. Rucio exerciser and the Storage view.
8. Probe and rider build-out (site-canary increments 8–9), extending
   node-level evidence to every node work reaches.

## Asks and open items

- JLab credential for EVGEN registration (standing ask).
- Payload event/CPU reporting (production implements; coordinated with
  the payload owners).
- corePower for the GREX queue; harvester refill and ceiling; the
  pull-mode trial (PanDA operations).
- Later: a non-interactive service credential for the dispatcher.
