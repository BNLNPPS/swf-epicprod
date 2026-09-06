# Retries and Reruns

Failed production work is recovered at one of four levels. They are
routinely confused with each other because three of them are called
some form of "retry" and all four end with jobs running again. They
differ in what they reuse, what they cost, and what has to still exist
for them to work at all.

The rule that orders them: **recover at the lowest level that still
works.** Level 1 costs nothing and is automatic. Level 4 always works
and costs the most. Levels 2 and 3 are cheap but depend on PanDA-side
remains that expire.

## The four levels

| Level | Operation | What runs again | Identity |
|---|---|---|---|
| 1 | Job attempt (automatic) | one failed job, inside its task | same task, same output file |
| 2 | **Add Another Retry** | nothing yet: it raises the per-job attempt ceiling so level 1 can continue | same task, same output file |
| 3 | **Restart Finished Task And Retry Failures** | the task's failed work, inside the existing task | same task, same output files |
| 4 | **Rerun Entire Task** / **Rerun Residual** | all work, or the undelivered remainder, as a new PanDA task | new physical task name, new output path namespace |

Levels 1 to 3 are native PanDA operations on an existing JEDI task.
Level 4 is a new PCS submission: PanDA and JEDI see an ordinary new
task and know nothing about the earlier attempt.

### Level 1 — the job attempt

JEDI regenerates a failed job until the task's attempt limit is
reached. Nothing is submitted or clicked; this is what "the task is
retrying" normally means. The submitter honors a `maxAttempt` carried
in the submission spec, but PCS puts none there, so the limit in force
today is JEDI's default.

Error-keyed rules can cut a job's attempts short of that limit; see
[Error-keyed retry rules](#error-keyed-retry-rules) below.

### Level 2 — Add Another Retry

`increase_attempt_nr(jediTaskID, 1)`. It raises the ceiling and
changes nothing else: use it when a task is still active and its
failures have exhausted the current limit. The compose page shows the
current job-level `nmax` when PanDA exposes it.

### Level 3 — Restart Finished Task And Retry Failures

`retry_task(jediTaskID, new_parameters={})`. The task re-executes its
failed work within itself. Use it when the task has reached a final
state and only the failed part should run again.

`new_parameters` is how a retry changes the task's resource
requirements (`ramCount`, walltime, ...): PanDA merges them into the
stored parameters and re-executes with them. A retry from the compose
page passes none.

### Level 4 — Rerun Entire Task and Rerun Residual

A new physical PanDA submission under the same logical campaign task,
named `<composed name>.tryN`. Use it when levels 2 and 3 cannot work:
the sandbox is purged, the task is beyond native retry, or the task is
`broken`. **Rerun Residual** is the same operation over only the
manifest rows whose output was never delivered, so delivered data is
not regenerated; see [Residual rerun](#residual-rerun) below.

## Names, namespaces, and the collision rule

A campaign task has two names. The **logical identity** is the PCS
composed name; the **physical attempt name** is the PanDA `taskName`
and BNL `outDS`. Attempt 1 uses the logical name; every later full
submission appends `.tryN`, allocated by PCS and recorded in
`PandaTasks.try_number`. `.tryN` is never part of the identity, and
delivery accounting unions across attempts at the physics
configuration, so a rerun needs no accounting change.

Science output is named by the payload from the work unit, not by
JEDI:

    RECO/<version>/<config>/<input dir below EVGEN>/<stem>.<chunk>.eicrecon.edm4eic.root

Three consequences, and they are the whole of the collision question:

- **A within-task retry (levels 1 to 3) writes the same file name.**
  That is correct and safe: the earlier attempt failed, so the name is
  free. The exception is a **ghost** — a file DID registered with no
  available replica anywhere, typically one COPYING replica from a
  failed upload. A ghost holds the name, and any later attempt on it
  fails with a duplicate DID.
- **A `.tryN` rerun cannot collide.** PCS sets `TAG_PREFIX=tryN` in the
  payload environment, so the try writes under `RECO/<version>/<config>/tryN/...`.
  Ghosts from earlier tries are in a different namespace and are
  irrelevant.
- **A resubmission made outside PCS collides.** It sets no prefix, so
  it writes the first-try paths again. This is the mechanism behind
  the ghost-and-duplicate-DID failures seen on legacy condor/PanDA
  resubmissions.

Ways out of a ghost: a `.tryN` rerun (new paths, always available), or
deletion of the replica rows by the JLab catalog's administrators — a
catalog action, not the storage site's. Never rename by hand; the
mechanism exists.

## What each level needs to still exist

Levels 2 and 3 regenerate jobs against the task's existing datasets and
sandbox, so both must survive:

**The sandbox tarball.** The PanDA server purges cache files untouched
for seven days, and a retry whose tarball is gone fails every job in
pre-process (executor error 5303). The doer therefore checks the task's
stored parameters for its tarball and HEADs the cache URL before any
retry-class operation, and **refuses** with a resubmit recommendation
when it is a confirmed 404 (`refused: sandbox_purged`). A check that
cannot reach a verdict lets the operation through rather than blocking
a legitimate retry. `--force-retry` bypasses the guard.

**The log datasets.** PanDA closes a task's BNL log datasets at
finalization, and the generic-VO JEDI plugins, unlike the ATLAS ones,
never reopen them; every log registration in a post-final retry would
fail with DDM error 200 ("is closed"). Every retry-class operation
therefore reopens the task's closed BNL datasets first, located by
their `task_id` metadata (never by name pattern, which collides across
sibling tasks), and gives each a fresh 30-day lifetime so the retried
task's logs do not expire on the original clock. The reopen report is
merged into the operation record; a reopen failure is reported, not
raised, and the retry proceeds.

### Keeping tasks retryable

The nightly `panda_sandbox_keepalive` step of the `catalog_sync` chain
(EPICPROD_OPS.md) touches the sandbox tarball of every task worth
keeping retryable, which resets the modification time the purge reads.
Candidates are epic-VO tasks in a non-final state, plus tasks
`finished`, `failed` or `exhausted` within SysConfig
`panda_sandbox_keepalive_final_days` (default 30). So native retry is
dependable for 30 days after a task goes final, not seven.

Detail worth knowing:

- `aborted` and `broken` tasks are deliberately not kept alive.
- One sandbox can serve several tasks; each tarball is touched once.
- A tarball already purged is recorded per task in the run inventory:
  that task is not natively retryable. This is established state, not
  a failure of the pass. API and authentication failures are errors.
- The same pass refreshes the BNL log datasets' lifetimes when they
  would expire inside the retention window.

## The task-state gates

PanDA accepts an operation only from certain task states. The
production surfaces check before sending, so an operator meets the
gate as a disabled control rather than as a rejection:

| Operation | One task | In bulk |
|---|---|---|
| Pause | any state except `finished`, `failed`, `done`, `aborted`, `broken`, `paused` | `running` |
| Resume | `paused`, `throttled`, `staging` | `paused` |
| Retry failures | `finished`, `failed`, `exhausted`, `aborted` | same |
| Finish | `running`, `paused`, `throttled`, `staging`, `exhausted`, `ready`, `pending`, `scouting`, `assigning`, `defined`, `registered` | same |

Pause and resume are gated more narrowly in bulk than singly; retry and
finish take the same states either way.

**`aborted` is the trap.** PanDA's command gate refuses a plain retry
of an aborted task. The operation therefore takes the reactivation
path automatically: a retry carrying `new_parameters` becomes an
`incexec`, which the gate accepts. The parameters restate the task's
own stored `taskPriority` verbatim, so nothing about the task changes.
PanDA answers that path with return code 3 and an explicit acceptance
message, which counts as accepted. When the stored parameters cannot
be read, a plain retry is sent instead and its refusal is recorded,
rather than risking a priority change.

**Kill is deliberately not offered.** It strands a task in `aborted`.
**Finish** is the stop verb: the task ends `finished`, completed output
is kept, and plain retry remains available.

## Error-keyed retry rules

Under the per-attempt count, the PanDA server's retry module applies
error-keyed rules: a `RETRYERRORS` row matches a failed job's error
source, code and diagnostic pattern and invokes a `RETRYACTIONS`
implementation (`no_retry`, `limit_retry`, and others). This is how a
known-hopeless error stops consuming attempts.

Two facts govern their use:

- A rule enforces only when **both** switches are set,
  `retryerrors.active` and `retryactions.active`. With either off the
  module logs what it would have done and does nothing.
- JEDI caches the rule set, so a change takes about an hour to take
  effect.

The rule-level switch is managed by
`swf-monitor/scripts/panda-retry-rules.py` (list by default, dry-run
without `--apply`). The action-level switch disables an action for
every rule that uses it and is deliberately not managed there. The
System page's PanDA Configuration section shows both tables live, and
the epic rule set with its rationale is in PANDA_ANCILLARY_AUDIT.md.

## Residual rerun

A full `.tryN` regenerates the whole workload, delivered rows included.
The residual rerun submits only the manifest rows whose output was
never delivered. It is an ordinary client-API `.tryN` submission whose
workload is the remainder; the residual computation is entirely
PCS-side. Design and derivation:
[JEDI_INTEGRATION.md § Residual rerun](JEDI_INTEGRATION.md).

**Delivered means registered and arrived.** A file counts only with an
AVAILABLE replica on at least one RSE, so a catalog entry whose data
never landed — a ghost — falls into the residual and is rerun.

The `PandaTasks` association records what the attempt covers:
`residual_of`, the row coverage ("M of N rows"), and the registered and
unarrived file counts per checked DID, so every surface can state it.

**It refuses rather than guesses.** The refusals, each with its reason:

- no recorded RECO outputs to diff against (run the Rucio update
  first, or rerun the entire task);
- an input resolving differently than at first submission;
- zero residual, every row registered and arrived;
- a background-mixed task, where `TAG_PREFIX` already shapes the output
  path (rerun the entire task instead);
- no recorded PanDA submission on the task itself.

**Rerun Residual** is preview-then-confirm: the preview
(`residual-preview`) performs the JLab listing on demand and returns
the coverage or the refusal, never in a page render.

For generation-only tasks the residual is target minus delivered
events, with delivered taken from registered outputs and the
event-measurement store and stated as floors. The `.tryN` advances the
output template `offset` past the prior try's serial range, so `${SN}`,
file names and sequence-derived seeds never collide. The operation
refuses when delivered events cannot be established.

### Legacy tasks

A task linked to PCS only by name match is not a PCS submission and
cannot be rerun until it is made one. It lacks a PanDA association
recorded on the task itself, a bound production configuration in place
of the import placeholder, and its dataset matched to its EVGEN input
in JLab Rucio. The compose page carries this as one control, **Move
this task to PCS**, which records the task's latest PanDA try as the
submission and binds the edition's Standard Production configuration.
It states what it will do or why it is blocked, and submits nothing.

## Surfaces

**Compose page** (one campaign task): Add Another Retry, Restart
Finished Task And Retry Failures, Rerun Entire Task, Rerun Residual.
The operations table with the "when used" reading is in
[EPICPROD_OPS.md](EPICPROD_OPS.md).

**PanDA task list** (many tasks at once): pause, resume, retry
failures, and finish as paced bulk requests, up to 5000 tasks. Commands
are sent with pacing and then verified on one shared clock; a bulk
retry verifies its tasks left the terminal states, while a single-task
retry from the compose page is fire-and-report.

**REST**, on the PCS task: `panda-add-retry`, `panda-retry-failures`,
`residual-preview`, `rerun-residual`, `rerun-entire-task`,
`adopt-readiness`.

**Under all of them** the web tier holds no credential. It queues a
request to the production operations agent, which runs
`scripts/panda-task-operation.py` under the production token and
returns the result over SSE (EPICPROD_OPS_AGENT.md).

## Choosing

- The task is active and out of attempts → **Add Another Retry**.
- The task is final and only the failures should run again →
  **Restart Finished Task And Retry Failures**.
- The retry was refused for a purged sandbox, or the task is `broken`
  → **Rerun Residual** if outputs are recorded, otherwise **Rerun
  Entire Task**.
- The task is `aborted` → **Restart Finished Task And Retry Failures**
  still applies; it takes the reactivation path by itself.
- Jobs are failing on one known error and eating attempts → a retry
  rule, not an operation.
- A ghost is holding an output name → **Rerun Residual**; it covers
  the row and writes in a new namespace.
