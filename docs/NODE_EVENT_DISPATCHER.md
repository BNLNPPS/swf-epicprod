# Node Event Dispatcher — event-range processing in fixed-lifetime allocations

Work-unit flavors across the system: TF slices (fast processing),
event ranges (this design; the established Event Service term), photon
bunches (GPU coprocessing).

A design for HPC production where the batch allocation's lifetime, not
the payload's needs, bounds the work: the payload becomes a dispatcher
that streams event ranges — contiguous spans of the job's assigned
EVGEN input events — to the allocation's cores until a deadline, then
packages and stages out everything complete and exits cleanly inside
the wall. The work quantum shrinks until the boundary cost is the few
ranges in flight at the deadline, and those are deferred to later
processing, never dropped. The implementation basis is the volunteer-GPU
coprocessor chain, adapted as the node-level dispatch manager.

## The problem, measured

At `NERSC_Perlmutter_epic` (14 days ending 2026-08-23), running ePIC
simulation/reconstruction over EVGEN inputs:

- Worker allocations run at most 4.05 hours (the Slurm walltime);
  each carries on the order of a hundred single-core payloads in one
  wave (306,220 job-worker mappings over 2,941 workers).
- Finished payloads need a median of 1.93 h (p90 2.49 h), but the
  duration tail crosses the wall: 27,265 jobs — 9% of the queue's
  terminal jobs — died as taskbuffer-300 (worker ended while the job
  ran), each burning its full allocation share and losing every event
  it had produced. That is roughly 106k core-hours of discarded work
  per 14 days, against 384k core-hours delivered by the finished
  jobs in the same window: about 22% of the core-hours consumed by
  finished and wall-killed jobs together, before counting the idle
  time of slots whose finished jobs wait out the wave's stragglers.
- The queue record advertises `maxtime` of 96 hours against the real
  4-hour ceiling, so every duration check in brokerage and pilot is
  blind. The pilot's multi-job window (`timefloor`) is no remedy: its
  fetch gate compares only elapsed time, so on a 4-hour wall it would
  admit second jobs destined to die at the wall with their output.
- Memory constrains occupancy to one of the two hardware threads per
  core for ePIC simulation payloads; the dispatcher inherits that
  worker-count budget rather than changing it.

The structural cause is a mismatch of quanta: the unit of dispatched
work (a multi-hour job) is comparable to the unit of computing (a
4-hour allocation).

Remeasured 2026-09-08 (the 14 days ending that day): 40,787 jobs
finished and 38,300 failed, 1,740 of them wall-kills, about 2% of the
terminal jobs against 9% in the August window. The queue record now
states `maxtime` 14400 s (Next steps 1).

## Design

One PanDA job occupies one node allocation: submission, brokerage,
pilot stage-in of the job's assigned EVGEN files, and stage-out are
unchanged. The payload is the dispatcher chain:

- **Event stream.** The dispatcher streams the job's assigned input
  events, quantized into ranges (file, first event, count), to N
  single-core simulation workers, handing each finisher its next
  range. N comes from the memory budget.
- **Deadline and drain.** At wall time minus a margin, dispatch stops
  and running ranges drain to completion — the margin is sized to the
  tail range duration plus packaging and stage-out, so abandonment is
  the exception (a pathological straggler), not the rule.
- **Deferral, never loss.** An abandoned or unprocessed range is named
  work: ranges still in flight or undispatched at the deadline return
  to the server's range bookkeeping and are re-dispatched to later
  allocations. This is a correctness requirement, not an
  optimization: ranges abandoned at a deadline are preferentially the
  slow ones, and slowness correlates with physics (multiplicity,
  topology), so dropping rather than deferring them would bias the
  sample against exactly those events.
- **Package: rolling merge, rolling stage-out.** The harness holds an
  uncompressed zip open and appends each range's output the moment it
  completes, deleting the member file — archive-only packing as a
  background trickle, no terminal merge latency, and no double-size
  disk peak (peak is the accumulated volume plus one range output).
  Every ~30 minutes the open zip closes and the harness registers it
  to JLab Rucio as an ordinary output file, on the payload data path
  and credential production uses today (the single-Rucio convention;
  PanDA stays out of the science data). Measured volumes: 0.46
  MB/event and ~552 MB per today's job give ~146 GB per full
  allocation, ~18 GB per 30-minute zip — and roughly 8 output files
  per allocation against ~260 today, so the dataset's file count
  falls thirty-fold. Each zip crosses the wire once, as the final
  product; nothing pre-ships and nothing ships twice.
- **Clean exit.** At the deadline the last zip closes, registers, and
  the job ends inside the wall with every completed range reported.
  The terminal cost is the last zip's transfer and the archive
  directory finalize — minutes. The taskbuffer-300 failure class
  disappears for these jobs except for genuine node failures.

### Completeness and accounting

Range completeness is owned by the **PanDA Event Service** — the
native mode, selected 2026-08-23 after a live probe verified it for
the epic VO.

Ordinary PanDA jobs are atomic over their inputs; the Event Service
is the long-established mode that is not: event ranges are
first-class JEDI state (`JEDI_Events` rows with per-range status,
attempts, and retry policy), enabled per task by standard parameters
(`nEventsPerWorker` switches the mode on; `nEsConsumers`,
`notDiscardEvents`, `esToNormal` refine it). The pilot reports each
range's disposition to the server, which retries unfinished ranges:
deferral-not-loss is this mode's native semantics. Range bookkeeping
rides the server machinery. The harness keeps the node-local fan-out
(a range per free slot to N workers, per-range completions back) and
packaging and output registration on the payload data path (Design §
Package; the pilot executor's own zip stage-out machinery goes unused
for science data). How ranges cross between the pilot and the harness
is the open design item recorded under The pilot side below and in
Open questions.

**The probe**
([task 39057](https://epic-devcloud.org/prod/panda/tasks/39057/),
`scripts/es-probe/` in swf-monitor, 2026-08-23): a tiny ES-mode task
against the production queue whose
payload did not speak a range channel. It verified, at the cost of a
few 2-minute single-core jobs: ES task refinement for epic
(`eventservice=1`, through the VO-neutral base refiner), range
creation at job generation (100 events into 10 `JEDI_Events` ranges),
dispatch and start on the site within about 5 minutes, and, on job
failure, cancellation of the attempt's ranges and their re-issue to a
successor job. No ATLAS-only gating appears at generation or retry.
The jobs failed before any Event Service code ran, on a pilot defect
read from the pilot source on 2026-09-08: the pilot's event-service
payload class calls the user module's payload-command builder without
its arguments, and the ePIC user module dereferences them (pilot
error 1310). The probe therefore verified the server side only; the
pilot's executor, its channel and the range round trip were not
exercised.

**The remaining verification (completed 2026-08-23, source-level):**

- *Storage activities*: the pilot resolves stage-out activities in
  order and uses the first with storages defined
  (`pilot/api/data.py`, `prepare_destinations`); with no `es_events`
  entry the ES request `['es_events', 'pw']` resolves to the
  production write storage — BNL_PROD_DISK_1 — with `es_failover`
  falling back the same way. The stage-out cadence is already
  configured: the pilot's `es_stageout_gap` maps from the queue field
  `zip_time_gap`, which the queue carries as 7200 s. No configuration
  work is needed.
- *Merge*: one ATLAS-only gap exists — registration of the
  pre-merge zips (`zipoutput` files, the `registerEsFiles` path) is
  implemented only in the ATLAS adder plugin; `AdderSimplePlugin`
  registers `output`/`log` types only.

**The pilot side (read 2026-09-08, pilot3 source):**

- Which executor runs an ES job is the site pilot environment's
  choice (`PILOT_ES_EXECUTOR_TYPE`, default `generic`), not the
  task's.
- The generic executor delivers ranges to the payload over a yampl
  socket (`PILOT_EVENTRANGECHANNEL`): the payload asks for events,
  receives one range as JSON (range id, LFN, first and last event,
  GUID, scope) and reports an output path per range. yampl is a C++
  library with Python bindings, absent from the site's pilot install,
  from pilot3's requirements and from our venv.
- The generic executor marks a range finished only after it has
  tarred the reported output and staged it (the `es_events` activity,
  every `es_stageout_gap`). A small per-range receipt as the reported
  output keeps that cost nil while the science data goes on the
  payload path.
- The fine-grained executor reports ranges bare, but its process class
  is an unfinished stub with no ePIC implementation.
- The pilot runs an Event Service payload outside the container unless
  the executor is the ATLAS Ray one (`pilot/util/container.py`, the
  overrule for event service jobs). The harness therefore runs on the
  host and starts its workers in the image itself, which is the
  coprocessor chain's shape already.
- Consumer regeneration is gated on the queue record: when a consumer
  ends with ranges unprocessed, the server creates a new consumer only
  if the queue's CRIC `jobseed` is `all` or `es`
  (`job_complex_module.py`, `ppEventServiceJob`); at a `std` queue it
  fails the job (`es_noevent`, code 125) and leaves the ranges. Deferral
  at the deadline therefore needs `jobseed = all` on the queue, a CRIC
  field the pilot's own queuedata does not reach.
  `NERSC_Perlmutter_epic` carries `std`; the test queue
  `NERSC_Perlmutter_epic_es` carries `all` (2026-09-10,
  [NERSC_PERLMUTTER.md](NERSC_PERLMUTTER.md)).
- The pilot defect the probe died on, above; fixed by PR 220 and
  confirmed on the first run below.

**First run of the pilot side (2026-09-09, `BNL_NPPS_GPU`,
[task 39564](https://epic-devcloud.org/prod/panda/tasks/39564/), job
2722535).** Under the canary pilot with PR 220, the generic executor and
yampl, the messaging library it hands event ranges through, installed on the host
([NPPS0_TEST_QUEUE.md](NPPS0_TEST_QUEUE.md)): the pilot chose the
event-service executor, built the payload command, started the generic
executor and its server-side communicator, opened the yampl server
socket (`EventService_EventRanges_<pid>`, context `local`) and exported
its name to the payload as `PILOT_EVENTRANGECHANNEL`. The payload did
not speak the channel, so no range was requested; the executor finished
cleanly and the pilot reported the job finished with zero events. The
server failed it on the `jobseed` gate above and cancelled the job's
one range (the probe task declared one event; the later probes declare
100, ten ranges of ten).

**The round trip (2026-09-09, task 39579, job 2722565).** With a
payload that speaks the channel (swf-monitor
`scripts/es-probe/es_range_client.py`) and two pilot fixes, the pilot
acquired ten ranges from the server, the payload processed and reported
each, the pilot tarred and staged the outputs to the queue's S3 store
and reported the ranges finished: all ten reached status done, the job
record carries ten events. Facts established on the way:

- The released pilot cannot obtain a range from a server with the
  refactored API (panda-server 1.0.0): its event service communicator
  calls `getEventRanges` and `updateEventRanges`, which no longer exist
  ("method getEventRanges is forbidden"). The communicator is ported to
  `api/v1/event/acquire_event_ranges` and `update_event_ranges` on the
  production team's pilot branch, with the api/v1 response envelope.
- The generic executor's event service stage-out raised a TypeError on
  every call since the `es_data.py` refactor of 2026-02 (positional
  argument to a keyword-only constructor); fixed on the same branch.
  Both fixes are [pilot3 PR 221](https://github.com/PanDAWMS/pilot3/pull/221),
  independent of PR 220.
- The server's bulk `update_event_ranges` builds its response as a set
  literal (`event_api.py`), so the response fails to serialize after the
  database update has landed; the pilot logs a warning and the ranges
  are updated. A one-line server fix.
- The pilot hands the channel to a non-Athena payload by string match:
  the payload command must contain `PILOT_EVENTRANGECHANNEL` (the
  pilot then prefixes the export); any other command is given the
  AthenaMP `--preExec` form.
- Event service stage-out needs the `es_events` activity declared on
  the queue with a copytool that reaches the store; undeclared, the
  pilot forces the `objectstore` copytool, a Rucio upload, which a
  non-RSE store refuses.
- A `noInput` task never reaches a finished job: the server's
  post-processing looks for done ranges on files of type `input` only
  (`job_complex_module.py`, `ppEventServiceJob`), so a pseudo-input
  task ends "all event ranges failed" with every range done. The
  production shape, ranges over the job's EVGEN input files, is the
  shape that finishes; it is the next probe.

**The defect and its fix (2026-09-09).** Two call sites build the
payload command without passing the pilot's arguments, and the ePIC
user module dereferences them, so an ePIC event-service job dies before
any event-service code runs. Both are on the event-service path alone;
the ordinary payload path passes the arguments and is unaffected, which
is why production has never seen this.

| where | now | fix |
|---|---|---|
| `pilot/control/payloads/eventservice.py:80` | `user.get_payload_command(job)` | `user.get_payload_command(job, args=self.get_args())` |
| `pilot/eventservice/workexecutor/plugins/baseexecutor.py:174` | `user.get_payload_command(job)` | `user.get_payload_command(job, args=self.args)` |

The first mirrors the working line in the generic payload path. It
reaches the arguments through a new `get_args()` on the parent
executor, beside that class's existing `get_job()`: the subclass could
have read the parent's private attribute directly, which resolves only
because both classes are named `Executor`, and an accessor removes a
rename that would break event-service payload construction silently.
The second class already holds `self.args` and uses it two lines
above.

Verified on 2026-09-09 against the released 3.14.3.3 by driving the
event-service executor with the user module replaced by a recorder:
unpatched, the user plugin is handed `args=None`, which is the
failure; patched, it is handed the pilot arguments object. The pilot's
own event-service test needs a live PanDA server and is no gate.

The fix is [pilot3 PR 220](https://github.com/PanDAWMS/pilot3/pull/220),
against the pilot's `next` branch, and goes into our canary pilot in
CVMFS ([OSG_SUBMISSION.md](OSG_SUBMISSION.md)) so it can be run before
a release carries it.

The pilot/harness boundary is therefore open (Open questions). The
rule for settling it is to resolve maximally in house: in the payload
and the ePIC user module we own, with the smallest change to the pilot
core.

**The merge resolution: no PanDA merge at all.** Epic production
moves no science data through PanDA — the payload self-registers
outputs to JLab Rucio under the single-Rucio convention — and the
dispatcher harness inherits that path: it rolls its own zips and
registers each to JLab as it closes (Design § Package). The Event
Service supplies range dispatch, bookkeeping, and retry; its own
zip stage-out and merge machinery (esmerge jobs, `onSiteMerging`,
`zipoutput` registration) go unused, which takes the ATLAS-only
gap out of the path entirely. One consequence to confirm in
the harness smoke run: the harness reports ranges finished over the
channel without attached zip records — the server-side update
handler treats the zip block as conditional
(`task_event_module.py`), so this should hold.

An epicprod coverage-layer alternative — manifest-declared range
completion diffed against campaign assignments by the produced-output
machinery of EPICPROD_DATA_LINEAGE.md — was considered and set aside
in favor of the native mechanism.

Per-range reporting also closes the events-source gap: each completed
range carries its exact event count, entering the measurement store as
a highest-provenance tier (`reported`) in place of today's
byte-size-class inference (CAMPAIGN_DELIVERY.md § The events source).

## Implementation basis: the coprocessor chain

The volunteer-GPU coprocessor workflow (`tools/worker/coprocessor/`,
WORK_UNIT_CONTRACT.md) is working, PanDA-verified code for the node
fan-out this design needs: the payload spawns its worker chain on the
node, the whole chain starting and ending with the job. The site
sees the same batch job and container as today: no services, no ports
beyond localhost, no new infrastructure; harvester submits one
standard mcore worker per allocation and sees output data only.

Under the native Event Service, the harness is the coprocessor chain
with the range transport swapped: the pilot owns server reporting, so
the harness's job is the node-local fan-out and the output path — take
ranges from the pilot, keep N single-core workers fed through the
inbox/outbox contract, hand per-range completions back, and run the
rolling zip merger with its 30-minute closes registered to JLab Rucio
(Design § Package). Two shapes of the pilot/harness boundary are on
the table (Open questions): the harness speaks the pilot's range
channel, which needs the generic executor and yampl, the messaging library
it hands ranges through, at the site; or an ePIC process class on the pilot side speaks the
harness's own contract, so the inbox/outbox contract is the interface
and the pilot needs no yampl.

The streaming reconstruction payload settles the shape of that
contract. EICrecon's managed socket (the fast-processing transformer,
swf-transform) takes an input file, a skip count and an event count
per request and answers with the events processed: the fields of a
range. The coprocessor's unit spec is the file-borne form of the same
request. One payload contract can serve every feeder: a broker slice,
a pilot range, an EJFAT push. Component disposition:

- The harness front end takes ranges from the pilot in the shape
  settled under Open questions (in the role `dispatcher.py` plays for
  the volunteer pool, where it remains in service unchanged); the
  in-node lease/retry of a died worker's range is preserved.
- `worker_agent.py` staging and the inbox/outbox/done contract:
  reused as-is, one work directory per core slot.
- A new contract executable wraps the simulation payload: consume a
  unit spec of the contract's input form extended with an event range,
  run the payload for that range, write outputs and counts per the
  contract. Specs are opaque to the staging layer; the contract is
  versioned for new source forms without schema change.
- The driver spawns N agent/executable pairs and mediates between
  the pilot's ranges and the unit contract. The per-unit counts and
  timing records and the in-job reference-unit check (a fixed-seed
  physics canary) carry over unchanged.

The new code is a few hundred lines against roughly eight hundred
proven ones; the substantial work is validation at the site.

## What it does not fix

- Genuine node failures (NODE_FAIL, ~2,900 of the 14-day 300s) still
  lose the node's open zip — at most ~30 minutes of one node's output
  under the rolling closes — and the server's range bookkeeping
  recovers the work. Node failure is ~1% of allocations; pre-shipping
  protection beyond the rolling closes is not justified for it.
- The memory-bound half-thread occupancy is a payload property,
  untouched here.

## Open questions

- The consumer contract for the packaged output: downstream steps
  reading range members from the zip container directly, versus an
  unpack step at the consuming site.
- The worker-shape configuration for one-job-per-allocation
  submission (the harvester and Globus Compute endpoint on the
  site's login node). The 4-hour allocation itself stays: short
  single-node requests backfill well — queue wait is 7 minutes at
  the median — and under event ranges the wall costs only the
  deadline margin.
- Whether the server accepts range-finished updates without attached
  zip records (the update handler reads the zip block conditionally;
  the harness reports ranges bare) — to confirm in the harness smoke
  run.
- The pilot/harness boundary: which side speaks which contract, the
  yampl messaging library the site's pilot lacks, and the pilot defect the
  probe died on (Completeness and accounting, The pilot side). Under
  discussion; the rule is to resolve maximally in house, in the
  payload and the ePIC user module.
- Queue-record hygiene independent of this design: `maxtime` states
  the real ceiling since 2026-09-08 (14400 s), so every duration check
  regains meaning.

## Benefits

- **Stops throwing away finished work.** Today the clock kills 9% of
  the jobs at NERSC — 27,265 jobs in two weeks, about 106,000
  core-hours, roughly a fifth of the core-hours consumed — and every
  event they produced is thrown away. With
  small work units, the clock can only catch the last few minutes of
  work, and even that gets re-run later.
- **Stops paying for idle cores.** Today a core that finishes its
  two-hour job sits idle while the slowest job in the allocation
  runs on; in an allocation that reaches its four-hour limit, that
  can idle half the capacity. With an event stream, every core stays
  busy to the deadline.
- **Keeps the physics unbiased.** The events the clock catches are
  preferentially the slow ones — high multiplicity — and dropping
  them would skew the sample. They are re-run instead.
- **Counts events exactly.** Every completed range reports exactly
  how many events it produced; the delivery bookkeeping stops
  estimating event counts from file sizes.
- **Thirty times fewer files.** About 8 files of ~18 GB per
  allocation instead of ~260 small ones — easier on the catalogs and
  the storage at both ends.
- **No end-of-job pileup.** Merging happens continuously as results
  arrive, so at the deadline there is nothing left to do but close
  and ship the last file — minutes, with no double-size disk spike
  on the node.
- **A node failure costs half an hour, not several hours.** About
  1% of nodes fail, today taking their jobs' completed work with
  them. With results shipped every 30 minutes, a failure loses at
  most half an hour of one node's output, and the affected events
  are re-run automatically.
- **One architecture, used twice.** This is the same work-unit
  design as the volunteer GPU coprocessor — the same contract and
  much of the same code, already proven in PanDA jobs. Building one
  improves the other, and both are facets of the same streaming
  approach.
- **A small build on proven parts.** No new site infrastructure and
  no changes to PanDA or harvester; on the pilot side, the ePIC
  pieces recorded under Completeness and accounting, resolved
  maximally in house. A few hundred new lines on top of working
  coprocessor code, driving established Event Service machinery.

## Next steps

- **0. Completed** — the Event Service probe
  ([task 39057](https://epic-devcloud.org/prod/panda/tasks/39057/),
  2026-08-23) verified the server side live for the epic VO: ES task
  refinement, range creation at job generation, dispatch and start
  on the site within minutes, and range-level cancel and re-issue on
  job failure. On that result the native Event Service was
  selected as the completeness mechanism. The pilot side was not
  exercised there: the jobs died on a pilot defect before any Event
  Service code ran. It was exercised on 2026-09-09 on the test queue
  (Completeness and accounting, First run of the pilot side).
- **0. Completed** — the remaining Event Service verification
  (2026-08-23, source-level): stage-out activities resolve to
  BNL_PROD_DISK_1 with no configuration work and the zip cadence is
  already set (`zip_time_gap`); the one ATLAS-only gap found
  (`zipoutput` registration exists only in the ATLAS adder) does not
  apply under the merge resolution: no PanDA merge, harness-rolled
  zips registered on the payload data path. Details in Completeness
  and accounting.
- **1. Completed** (2026-09-08) — the queue record states `maxtime`
  14400 s, the real allocation ceiling.
- **2.** Arrange the worker-shape change with the site operator:
  one mcore job per allocation, on the existing 4-hour wall. The
  other site facts are recorded: harvester runs on the site login
  node via Globus Compute, the wall request is 4 hours, and queue
  wait is 7 minutes at the median (p90 about 12 hours). The queue
  record needs `jobseed = all` in CRIC (The pilot side): done on the
  Perlmutter test queue `NERSC_Perlmutter_epic_es` (2026-09-10), and
  on the production queue with the worker-shape change. How a pilot
  starts at the site, and the arrangement under which production
  operations controls the pilot on the test queue, are in
  [NERSC_PERLMUTTER.md](NERSC_PERLMUTTER.md).
- **3.** Build the node harness: the pilot-side bridge in the shape
  settled under Open questions, the range-form unit spec, the
  simulation contract executable, the N-pair driver, and the rolling
  zip merger with 30-minute closes
  registered to JLab Rucio; smoke-run as a loopback on a development
  host, the coprocessor pattern, then as Event Service jobs on the
  `BNL_NPPS_GPU` test queue ([NPPS0_TEST_QUEUE.md](NPPS0_TEST_QUEUE.md)),
  where the pilot side, the boundary and the harness are proved
  before any other queue is involved. The smoke run also confirms
  bare range-finished reporting (Open questions).
- **4.** Settle the packaged-output consumer contract with the
  downstream processing step.
- **5.** Run a first task on the queue — a few one-node allocations
  under the Event Service, validated against a reference sample —
  then scale and retire the wave model.

## Related

- [NPPS0_TEST_QUEUE.md](https://github.com/BNLNPPS/swf-epicprod/blob/main/docs/NPPS0_TEST_QUEUE.md)
  — the test queue where the pilot side of this design runs first:
  the canary pilot, the Event Service path, the boundary, the harness.
- [WORK_UNIT_CONTRACT.md](https://github.com/BNLNPPS/swf-epicprod/blob/main/docs/WORK_UNIT_CONTRACT.md)
  and
  [VOLUNTEER_GPU_PLAN.md](https://github.com/BNLNPPS/swf-epicprod/blob/main/docs/VOLUNTEER_GPU_PLAN.md)
  — the coprocessor work-unit loop this design adapts: an executable
  may exit cleanly at any unit boundary, and an unfinished unit is
  simply reprocessed. Code:
  [tools/worker/coprocessor/](https://github.com/BNLNPPS/swf-epicprod/tree/main/tools/worker/coprocessor).
- [EPICPROD_EVGEN_INPUTS.md](https://github.com/BNLNPPS/swf-epicprod/blob/main/docs/EPICPROD_EVGEN_INPUTS.md)
  — the EVGEN inputs production consumes, and the definitions cost
  model.
- [EPICPROD_DATA_LINEAGE.md](https://github.com/BNLNPPS/swf-epicprod/blob/main/docs/EPICPROD_DATA_LINEAGE.md)
  — the produced-output coverage machinery; the basis of the
  considered coverage-layer alternative.
- [CAMPAIGN_DELIVERY.md](https://github.com/BNLNPPS/swf-epicprod/blob/main/docs/CAMPAIGN_DELIVERY.md)
  — the delivered-data record and the events source that per-range
  reporting feeds.
- [JEDI_INTEGRATION.md](https://github.com/BNLNPPS/swf-epicprod/blob/main/docs/JEDI_INTEGRATION.md)
  — submission design; payload-side data handling.
- [PANDA_ANCILLARY_AUDIT.md](https://github.com/BNLNPPS/swf-epicprod/blob/main/docs/PANDA_ANCILLARY_AUDIT.md)
  — the Event Service's integration status for the epic VO.
- [swf-transform](https://github.com/BNLNPPS/swf-transform)
  — the fast-processing transformer and the EICrecon managed-socket
  payload interface, the reconstruction form of the unit contract;
  described in the WFMS documentation under Streaming Reconstruction
  Integration.
