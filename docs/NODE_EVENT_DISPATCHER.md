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
  per 14 days, before counting the idle time of slots whose finished
  jobs wait out the wave's stragglers.
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
`notDiscardEvents`, `esToNormal` refine it). The pilot version
already deployed on this queue carries the complete generic ES
executor: it delivers ranges to the payload over a socket channel
(`PILOT_EVENTRANGECHANNEL`) and reports each range's disposition to
the server, which retries unfinished ranges. Deferral-not-loss is
this mode's native semantics. The node harness therefore speaks the
range channel instead of running its own dispatcher — request a range per free slot, fan out
to N workers, emit per-range completions — and range bookkeeping
rides the server machinery, while packaging and output registration
stay with the harness on the payload data path (Design § Package;
the pilot executor's own zip stage-out machinery goes unused for
science data).

**The probe**
([task 39057](https://epic-devcloud.org/prod/panda/tasks/39057/),
`scripts/es-probe/` in swf-monitor, 2026-08-23): a tiny ES-mode task
against the production queue whose
payload deliberately did not speak the range channel. It verified,
at the cost of two 2-minute single-core jobs: ES task refinement for
epic (`eventservice=1`, through the VO-neutral base refiner), range
creation at job generation (100 events into 10 `JEDI_Events` ranges),
dispatch and start on the site within about 5 minutes, and — on the
payload failure — cancellation of the attempt's ranges and their
re-issue to a successor job. No ATLAS-only gating appears at
generation or retry.

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
with the range transport swapped: the pilot's ES executor owns range
delivery (the socket channel) and server reporting, so the harness's
job is the node-local fan-out and the output path — receive ranges
from the channel, keep N single-core workers fed through the
inbox/outbox contract, return per-range completions to the channel,
and run the rolling zip merger with its 30-minute closes registered
to JLab Rucio (Design § Package). Component disposition:

- The harness front end speaks the pilot range channel (in the role
  `dispatcher.py` plays for the volunteer pool, where it remains in
  service unchanged); the in-node lease/retry of a died worker's
  range is preserved.
- `worker_agent.py` staging and the inbox/outbox/done contract:
  reused as-is, one work directory per core slot.
- A new contract executable wraps the simulation payload: consume a
  unit spec of the contract's input form extended with an event range,
  run the payload for that range, write outputs and counts per the
  contract. Specs are opaque to the staging layer; the contract is
  versioned for new source forms without schema change.
- The driver spawns N agent/executable pairs and mediates between
  the range channel and the unit contract. The per-unit counts and
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
- Site facts that size the parameters: the allocation walltime and
  whether it can lengthen, and the worker-shape configuration for
  one-job-per-allocation submission.
- Whether the server accepts range-finished updates without attached
  zip records (the update handler reads the zip block conditionally;
  the harness reports ranges bare) — to confirm in the harness smoke
  run.
- Queue-record hygiene independent of this design: `maxtime` should
  state the real ceiling so every duration check regains meaning.

## Benefits

- **Stops throwing away finished work.** Today the clock kills 9% of
  the jobs at NERSC — 27,265 jobs in two weeks, about 106,000
  core-hours — and every event they produced is thrown away. With
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
  no changes to PanDA, the pilot, or harvester: a few hundred new
  lines on top of working coprocessor code, driving established
  Event Service machinery.

## Next steps

- **0. Completed** — the Event Service probe
  ([task 39057](https://epic-devcloud.org/prod/panda/tasks/39057/),
  2026-08-23) verified the server side live for the epic VO: ES task
  refinement, range creation at job generation, dispatch and start
  on the site within minutes, and range-level cancel and re-issue on
  payload failure. On that result the native Event Service was
  selected as the completeness mechanism.
- **0. Completed** — the remaining Event Service verification
  (2026-08-23, source-level): stage-out activities resolve to
  BNL_PROD_DISK_1 with no configuration work and the zip cadence is
  already set (`zip_time_gap`); the one ATLAS-only gap found
  (`zipoutput` registration exists only in the ATLAS adder) does not
  apply under the merge resolution: no PanDA merge, harness-rolled
  zips registered on the payload data path. Details in Completeness
  and accounting.
- **1.** Correct the queue record: `maxtime` to the real allocation
  ceiling. Independent of the rest and immediately useful.
- **2.** Obtain the site facts: allocation walltime and its
  prospects, and the worker-shape configuration for
  one-job-per-allocation submission.
- **3.** Build the node harness: the pilot-range-channel front end,
  the range-form unit spec, the simulation contract executable, the
  N-pair driver, and the rolling zip merger with 30-minute closes
  registered to JLab Rucio; smoke-run as a loopback on a development
  host, the coprocessor pattern. The smoke run also confirms bare
  range-finished reporting (Open questions).
- **4.** Settle the packaged-output consumer contract with the
  downstream processing step.
- **5.** Run a first task on the queue — a few one-node allocations
  under the Event Service, validated against a reference sample —
  then scale and retire the wave model.

## Related

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
