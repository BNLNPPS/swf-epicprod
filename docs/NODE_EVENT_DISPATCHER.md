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
- **Package.** Completed range outputs concatenate into uncompressed
  zip — archive-only packing at disk speed. The pilot's Event Service
  machinery performs this packing and stages the zip out periodically
  (`es_stageout_gap`), a handful of files per allocation. No
  many-small-files pressure on the HPC filesystem, no scattered
  outputs, and periodic stage-out bounds what a node failure can
  take.
- **Clean exit.** The job ends inside the wall with every completed
  range staged and reported. The taskbuffer-300 failure class
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
(`PILOT_EVENTRANGECHANNEL`), collects per-range outputs, packs them
with archive-only zip, stages the zip out periodically
(`es_stageout_gap` in the queue data) with storage failover, and
reports each range's disposition to the server, which retries
unfinished ranges. Deferral-not-loss is this mode's native semantics,
in production for well over a decade. The node harness therefore
speaks the range channel instead of running its own dispatcher —
request a range per free slot, fan out to N workers, emit per-range
completions — and packaging, stage-out, and range bookkeeping ride
the pilot and server machinery.

**The probe** (task 39057, `scripts/es-probe/` in swf-monitor,
2026-08-23): a tiny ES-mode task against the production queue whose
payload deliberately did not speak the range channel. It verified,
at the cost of two 2-minute single-core jobs: ES task refinement for
epic (`eventservice=1`, through the VO-neutral base refiner), range
creation at job generation (100 events into 10 `JEDI_Events` ranges),
dispatch and start on the site within about 5 minutes, and — on the
payload failure — cancellation of the attempt's ranges and their
re-issue to a successor job. The feared ATLAS-shaped corners at
generation and retry are absent.

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
- *Merge*: one true ATLAS-shaped corner exists — registration of the
  pre-merge zips (`zipoutput` files, the `registerEsFiles` path) is
  implemented only in the ATLAS adder plugin; `AdderSimplePlugin`
  registers `output`/`log` types only. The open path for epic is
  **on-site merging**: `onSiteMerging` with an `esmergeSpec` in the
  task parameters — handled in the VO-neutral base refiner and core
  job generator — runs the merge inside the ES job on the node, and
  the final outputs are ordinary files the simple adder registers
  normally. This is the selected merge form; it also matches the
  design's node-side packaging shape. A `zipoutput` extension to the
  simple adder remains a fallback if separate merge jobs are ever
  wanted.

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
node, the whole chain living and dying with the job. The site sees
the same batch job and container as today: no services, no ports
beyond localhost, no new infrastructure; harvester submits one
standard mcore worker per allocation and sees output data only.

Under the native Event Service, the harness is the coprocessor chain
with the range transport swapped: the pilot's ES executor owns range
delivery (the socket channel), packaging, periodic stage-out, and
server reporting, so the harness's job is the node-local fan-out —
receive ranges from the channel, keep N single-core workers fed
through the inbox/outbox contract, and return per-range completions
to the channel. Component disposition:

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
  lose the node's completed-but-unstaged ranges; periodic Event
  Service stage-out bounds that exposure, and the server's range
  bookkeeping recovers the work. Node failure is ~1% of allocations.
- The memory-bound half-thread occupancy is a payload property,
  untouched here.

## Open questions

- The consumer contract for the packaged output: downstream steps
  reading range members from the zip container directly, versus an
  unpack step at the consuming site.
- Site facts that size the parameters: the allocation walltime and
  whether it can lengthen, and the worker-shape configuration for
  one-job-per-allocation submission.
- Under on-site merging, whether the periodic pre-merge zip
  stage-out still runs (the node-failure exposure bound) or the
  range outputs stay node-local until the in-job merge — to confirm
  in the harness smoke run.
- Queue-record hygiene independent of this design: `maxtime` should
  state the real ceiling so every duration check regains meaning.

## Next steps

Completed steps do not disappear; they move to number 0 with a
Completed leader, so the record shows what has been done and proven.

- **0. Completed** — the Event Service probe (task 39057,
  2026-08-23) verified the server side live for the epic VO: ES task
  refinement, range creation at job generation, dispatch and start
  on the site within minutes, and range-level cancel and re-issue on
  payload failure. On that result the native Event Service was
  selected as the completeness mechanism.
- **0. Completed** — the remaining Event Service verification
  (2026-08-23, source-level): stage-out activities resolve to
  BNL_PROD_DISK_1 with no configuration work and the zip cadence is
  already set (`zip_time_gap`); merge is taken as on-site merging
  (`onSiteMerging` + `esmergeSpec`, VO-neutral), avoiding the one
  ATLAS-only corner found (`zipoutput` registration in the ATLAS
  adder). Details in Completeness and accounting.
- **1.** Correct the queue record: `maxtime` to the real allocation
  ceiling. Independent of the rest and immediately useful.
- **2.** Obtain the site facts: allocation walltime and its
  prospects, and the worker-shape configuration for
  one-job-per-allocation submission.
- **3.** Build the node harness: the pilot-range-channel front end,
  the range-form unit spec, the simulation contract executable, and
  the N-pair driver; smoke-run as a loopback on a development host,
  the coprocessor pattern. The smoke run also settles the on-site
  merge and periodic stage-out interplay (Open questions).
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
