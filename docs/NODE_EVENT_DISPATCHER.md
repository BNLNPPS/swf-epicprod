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
  work: the ranges still in flight or undispatched at the deadline are
  recorded in the manifest as unprocessed and are re-dispatched in
  later allocations. This is a correctness requirement, not an
  optimization: ranges abandoned at a deadline are preferentially the
  slow ones, and slowness correlates with physics (multiplicity,
  topology), so dropping rather than deferring them would bias the
  sample against exactly those events.
- **Package.** Completed range outputs concatenate into a single
  uncompressed zip — archive-only packing at disk speed — one output
  file per allocation with its name fixed at job generation, plus a
  manifest recording each range's identity, event count, and
  completion. No many-small-files pressure on the HPC filesystem, no
  scattered outputs; the file count reaching Rucio is per-allocation.
- **Clean exit.** The job ends `finished` inside the wall; the pilot
  stages out and registers the one output through the normal path.
  The taskbuffer-300 failure class disappears for these jobs except
  for genuine node failures.

### Completeness and accounting

Two mechanisms can own range completeness; the payload-side harness is
the same work either way.

**The PanDA Event Service, the native mode.** Ordinary PanDA jobs are
atomic over their inputs; the Event Service is the long-established
mode that is not: event ranges are first-class JEDI state
(`JEDI_Events` rows with per-range status, attempts, and retry
policy), enabled per task by standard parameters (`eventService`,
`nEventsPerWorker`, `nEsConsumers`, `notDiscardEvents`, `esToNormal`).
The pilot version already deployed on this queue carries the complete
generic ES executor: it delivers ranges to the payload over a socket
channel (`PILOT_EVENTRANGECHANNEL`), collects per-range outputs, packs
them with archive-only zip, stages the zip out periodically
(`es_stageout_gap` in the queue data) with storage failover, and
reports each range's disposition to the server, which retries
unfinished ranges. Deferral-not-loss is this mode's native semantics,
in production for well over a decade. Under this option the node
harness speaks the range channel instead of running its own
dispatcher — request a range per free slot, fan out to N workers,
emit per-range completions — and packaging, stage-out, and range
bookkeeping ride the pilot and server machinery. To verify for epic:
the JEDI ES generation and merge-job paths for a non-ATLAS VO (the
ancillary audit's pattern predicts ATLAS-shaped corners), and the
`es_events`/`es_failover` storage-activity mapping to BNL storage.

**Coverage-layer completeness, the epicprod-side alternative.** The
manifest states exactly which ranges completed; the coverage
machinery (the produced-output mapping of EPICPROD_DATA_LINEAGE.md
and the delivery record) diffs completed ranges against campaign
assignments and issues unprocessed remainder as follow-up tasks. A
deferred slow range re-runs with a full fresh time budget, removing
the bias by construction. This path has no PanDA-side unknowns and
keeps all state in systems the production domain owns.

The manifest also closes the events-source gap: the dispatcher reports
exactly what it produced, entering the measurement store as a
highest-provenance tier (`reported`) in place of today's
byte-size-class inference (CAMPAIGN_DELIVERY.md § The events source).

## Implementation basis: the coprocessor chain

The volunteer-GPU coprocessor workflow (`tools/worker/coprocessor/`,
WORK_UNIT_CONTRACT.md) is working, PanDA-verified code for exactly
this shape, and its self-contained driver mode — the PanDA payload
spawns dispatcher, agents, and executables on localhost, the whole
chain living and dying with the job — is the deployment model here.
The site sees the same batch job and container as today: no services,
no ports beyond localhost, no new infrastructure. No pilot changes (the
zip is an ordinary declared output), no harvester changes (one
standard mcore worker per allocation, output data only).

Component disposition:

- `dispatcher.py` (stdlib + sqlite): reused essentially verbatim. Its
  lease-TTL re-queue gives in-node retry — a worker process that dies
  or hangs has its range re-served within the allocation.
- `worker_agent.py`: reused as-is, one agent and work directory per
  core slot; the same unmodified chain then serves both deployments —
  remote dispatcher for the volunteer pool, localhost for the node.
- A new contract executable wraps the simulation payload: consume a
  unit spec of the contract's input form extended with an event range,
  run the payload for that range, write outputs and counts per the
  contract. Specs are opaque to dispatcher and agent; the contract is
  versioned for new source forms without schema change.
- Driver deltas: spawn N agent/executable pairs instead of one;
  deadline semantics become stop-dispatch, drain, and succeed with
  the manifest; outbox-to-zip packaging; a unit builder over the
  job's assigned input ranges. The per-unit counts and timing records
  and the in-job reference-unit check (a fixed-seed physics canary)
  carry over unchanged.

The new code is a few hundred lines against roughly eight hundred
proven ones; the substantial work is validation at the site and the
coverage re-dispatch loop.

## What it does not fix

- Genuine node failures (NODE_FAIL, ~2,900 of the 14-day 300s) still
  lose the node's completed-but-unstaged ranges; the Event Service
  option's periodic stage-out bounds that exposure natively, the
  coverage-layer option needs a later increment for it, and the
  deferral machinery recovers the work either way. Node failure is
  ~1% of allocations.
- The memory-bound half-thread occupancy is a payload property,
  untouched here.

## Open questions

- The consumer contract for the packaged output: downstream steps
  reading range members from the zip container directly, versus an
  unpack step at the consuming site.
- Site facts that size the parameters: the allocation walltime and
  whether it can lengthen, and the worker-shape configuration for
  one-job-per-allocation submission.
- The completeness-mechanism decision: verify the JEDI Event Service
  generation and merge-job paths for the epic VO — the deciding fact
  between native ES and coverage-layer completeness. If coverage-layer:
  assignment granularity for re-dispatched ranges and their manifest
  lineage.
- Queue-record hygiene independent of this design: `maxtime` should
  state the real ceiling so every duration check regains meaning.

## Next steps

1. Verify the JEDI Event Service generation and merge-job paths for
   the epic VO, in the source and the live server configuration, and
   the `es_events`/`es_failover` storage-activity mapping — the
   completeness-mechanism decision.
2. Correct the queue record: `maxtime` to the real allocation
   ceiling. Independent of the rest and immediately useful.
3. Obtain the site facts: allocation walltime and its prospects, and
   the worker-shape configuration for one-job-per-allocation
   submission.
4. Build the node harness against the coprocessor contract: the
   range-form unit spec, the simulation contract executable, and the
   N-pair driver; smoke-run as a loopback on a development host, the
   coprocessor pattern.
5. Settle the packaged-output consumer contract with the downstream
   processing step.
6. Run a first task on the queue — a few one-node allocations under
   the chosen completeness mechanism, validated against a reference
   sample — then scale and retire the wave model.

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
  — the produced-output coverage machinery the deferral loop builds
  on.
- [CAMPAIGN_DELIVERY.md](https://github.com/BNLNPPS/swf-epicprod/blob/main/docs/CAMPAIGN_DELIVERY.md)
  — the delivered-data record and the events source the manifest
  reports into.
- [JEDI_INTEGRATION.md](https://github.com/BNLNPPS/swf-epicprod/blob/main/docs/JEDI_INTEGRATION.md)
  — submission design; payload-side data handling.
- [PANDA_ANCILLARY_AUDIT.md](https://github.com/BNLNPPS/swf-epicprod/blob/main/docs/PANDA_ANCILLARY_AUDIT.md)
  — the Event Service's integration status for the epic VO.
