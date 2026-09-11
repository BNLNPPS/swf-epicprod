# Segfault diagnosis

Payload crashes (segmentation faults and related signal deaths) in
ePIC production jobs, found from the production record, catalogued by
signature, reproduced as standalone runs, diagnosed with LLM assistance
where the evidence allows, and handed to software experts as
self-contained reproductions where it does not. This document is the
plan and the implementation specification; each component names the
files it touches and the check that shows it working.

Related: [EPICPROD_PAYLOAD.md](EPICPROD_PAYLOAD.md) (the payload, its
exit codes and report), [EPICPROD_RETRIES.md](EPICPROD_RETRIES.md)
(residual rerun), [JOB_REPORTING.md](JOB_REPORTING.md) (the report
channel out of a running job),
[ERROR_ATTRIBUTION.md](https://github.com/BNLNPPS/swf-monitor/blob/main/docs/ERROR_ATTRIBUTION.md)
(the evidence ladder and representative-job digs),
[EPICPROD_ASSESSMENTS.md](EPICPROD_ASSESSMENTS.md) and
[EPICPROD_ASSESSMENTS_V1.md](EPICPROD_ASSESSMENTS_V1.md) (the LLM
harness and the corun-ai job contract), site-canary
[IMPLEMENTATION.md § Payload canaries](https://github.com/BNLNPPS/site-canary/blob/main/docs/IMPLEMENTATION.md)
(one manifest row as a canary task).

## What the record holds

A crash in the payload reaches the PanDA record as the payload's exit
code, stored on every failed job as `transexitcode`: 139 for SIGSEGV,
134 for SIGABRT, 135 for SIGBUS, 136 for SIGFPE, because `run.sh` runs
every stage under `set -Euo pipefail` with an ERR trap that exits with
the failing command's status (`swf_epicprod/payload/run.sh`, line 2 and
line 38), and prmon returns the status of the program it wraps. The
pilot labels these jobs with whatever it last read from stderr, so in
the record they appear as pilot error 1305 ("bind mounting", "unknown
groupid") and are invisible to a reader of the pilot label. The
correction service (ERROR_ATTRIBUTION.md) already presents the exit
code through that label.

Over the 65 days to 2026-09-11 the archive holds 835,954 failed jobs,
of which 116,261 exited 139, 71 exited 134 and 2 exited 135. Timing and
spread, with no trace read, separate them into classes:

| Class | Population | Rate of finished plus failed | Time to death | Node spread |
|---|---|---|---|---|
| Storm | nine 26.07.1 DIS NC tasks at 10x275 and 18x275 and one pythia8 NC task, 2026-08-02 to 08-11, about 108,000 jobs | 24% to 67% per task | 10th percentile and median both 14 minutes (one task 3 to 5 minutes) | 1,500 to 2,000 hosts at one OSG queue; one task on 8 hosts at GREX |
| Configuration dead | beam-gas and synchrotron background tasks, about 5,100 jobs | 99% to 100% | fixed per task: 5 to 6 minutes, or 33 to 140 minutes | many hosts |
| Sparse | about 45 tasks across DIS NC and CC, SIDIS, BeAGLE and pythia8, at Perlmutter, OSG, GREX and Google, about 2,000 jobs | 0.1% to 5% | varies widely within a task | about one crash per host |
| Abort | four test tasks, 71 jobs, exit 134 | 40% to 73% | | |

A storm with a fixed time to death across two thousand nodes is one
deterministic cause; a task crashing every job cannot run its
configuration at all; a sparse class with variable time to death and no
node concentration is where event-dependent crashes live, and where a
campaign silently loses chunks with a physics shape. The catalog exists
to make these distinctions from the record before any log is read, and
to carry each signature from first sighting to a verdict.

## Definitions

**Crash class.** A failed job whose `transexitcode` is 128 plus a
signal number: 134, 135, 136, 139. The exit-code registry in
EPICPROD_PAYLOAD.md gains this row: "128+N: the stage's program died
on signal N; the stage log names the stage." The payload does not map
these to its own codes, so that the signal number survives.

**Signature.** A crash signature has three levels, and a catalog entry
is promoted through them as evidence arrives:

- *Record level*: exit code × PanDA task. Every crash has this the day
  it happens. Tasks are grouped for display by campaign edition,
  physics process family (the composed name's process fields), time to
  death band and rate band, which is what separates the classes above,
  but grouping at this level is a display hypothesis and never merges
  entries.
- *Trace level*: the crashing frame, read from the payload's stderr
  (the library and function at the top of the backtrace, the stage,
  and the program). Tasks sharing a frame merge into one entry.
- *Reproduction level*: the outcome of running the crashed row again:
  reproduced at the production site, reproduced at a reference site,
  not reproduced, or platform-dependent.

**Row.** The manifest row a job ran: `file,ext,nevents,ichunk`
(`pcs/manifests.py`). The in-job dispatcher runs row *n* for job
sequence number *n* (`evgen_job_dispatcher.py`, `run_row`), the
payload seeds the simulation with `ichunk + 1`
(`run.sh`, line 182), enables the per-event seed
(`--random.enableEventSeed`) and mixes the background merger's seed
from the input name and the chunk seed. A row is therefore sufficient
to rerun the job's work: the same image, input file, chunk and seed.

## Components

### 1. Inventory builder

`swf-monitor/scripts/segfault-inventory.py`. A script run under the
monitor's virtual environment with Django configured, reading the
PanDA database through `connections['panda']` (the connection
`monitor_app/panda/queries.py` uses) and writing to swfdb through the
ORM. It runs standalone for the campaign back-fill and as a nightly
chain step for top-ups.

Selection: jobs in `doma_panda.jobsarchived4` and
`doma_pandaarch.jobsarchived` with `jobstatus = 'failed'` and
`transexitcode IN ('134','135','136','139')`, `modificationtime`
inside the window. The union of the two tables is required: the
archive holds the campaign, the live table holds the last days.

Per job, the builder records one `EpicProdJob` row
(`monitor_app/models.py`, table `swf_epicprod_jobs`; no schema
change), `phase = 'payload_crash'`, `status = 'failed'`,
`failure_summary` a one-line reading ("simulation died on signal 11
after 14 minutes at BNL_OSG_EPIC_PROD_1"), `jeditaskid`, `prod_task`
resolved through `PandaTasks` by JEDI task id when an association
exists, `seq_number` from the job parameters, and in `data`:

```
exit_code, signal, computingsite, modificationhost, starttime, endtime,
minutes (endtime − starttime), maxrss_mb, maxpss_mb, cpuconsumptiontime,
piloterrorcode, piloterrordiag (as stored), taskname, jobname,
attemptnr, row (file, ext, nevents, ichunk, when resolved),
stage (from the payload digest in jobmetrics when present),
trace_status (unknown | found | absent | log_unavailable)
```

The sequence number is the first argument of the dispatcher exec in
the job's parameters (`doma_panda.jobparamstable`, joined by
`pandaid`); the row is `expand(record_of(panda_tasks))[seq − 1]` when
the attempt's manifest record exists (`pcs/manifests.py`), else left
unresolved and counted. Legacy attempts submitted outside PCS carry
the run script's argument list in the job parameters instead; the
builder parses `file ext nevents ichunk` from it when the dispatcher
form is absent. The implementer confirms both forms on one
representative job of each kind before relying on them.

Per PanDA task, the builder computes the record-level signature and
upserts a `CrashSignature` row (below): counts, first and last seen,
the finished and failed totals of the task from the same tables, rate,
10th and 50th percentile time to death, distinct hosts, distinct
sites, the configuration (composed name, `ProdConfig` and image when
the task is associated), and the class heuristic:

- `configuration_dead`: rate ≥ 95%;
- `storm`: rate ≥ 20% and the interquartile range of time to death
  under 20% of the median;
- `sparse`: rate < 5%;
- `abort`: exit 134 regardless;
- otherwise `mixed`, which is a class to look at, not a verdict.

Thresholds are SysConfig keys (`segfault_class_*`) with these defaults,
visible on the System page as every SysConfig key is.

Invocation: `segfault-inventory.py --since 2026-07-01` for the campaign
back-fill (minutes; the 65-day aggregate query ran in about two
minutes), `--days 3` for the nightly. The nightly window overlaps
deliberately; upserts are keyed on `pandaid`, so a job seen twice is
one row. Every run logs one `segfault_inventory` action to the
epicprod action stream (`monitor_app/epicprod_logging.py`, sublevel
`normal`) carrying the window, jobs seen, rows added, rows unresolved
and signatures touched; an error is an action with outcome `error`,
never a silent exit.

Chain step: `('segfault_inventory', self._do_segfault_inventory)` in
`agents/epicprod_ops_agent.py`, `_do_catalog_sync`, placed after
`batch_log_learn` and before the first Rucio step, so a catalog stall
cannot cost the day's crash record. The doer pattern is
`_do_panda_sandbox_keepalive`. EPICPROD_OPS.md § Nightly catalog sync
gains the step in its chain sentence.

Acceptance: after the back-fill, the signature counts for exit 139
sum to the archive's count for the same window (the SQL in the
appendix), and the job page of a representative crashed job shows its
row.

### 2. Signature record

`CrashSignature` in `monitor_app/models.py`, table
`swf_crash_signatures`. The migration is created on approval as the
last step of the stage that first needs it (stage 2 below); stage 1
runs with `EpicProdJob` rows alone.

```
key              CharField, unique: 'exit139:task38661' at record level,
                 'exit139:frame:<sha>' at trace level
level            record | trace | reproduction
exit_code        int
signal           int
class_hint       storm | configuration_dead | sparse | abort | mixed
tasks            JSON list of {jeditaskid, taskname, crashes, finished,
                 failed, first_seen, last_seen}
configuration    JSON: campaign, edition, process family, ProdConfig id,
                 container image, detector version, payload version
sites            JSON list of {site, crashes, hosts}
crashes          int
rate             float
minutes_p10, minutes_p50   float
first_seen, last_seen      datetime
rows_lost        int (rows with no delivered output among the crashed)
events_lost      int
trace            JSON: program, stage, top frames (bounded), source
                 job, trace_status
reproduction     JSON list of {pandaid, queue, jedi_task_id, outcome,
                 minutes, verdict_time}
status           new | digging | traced | reproducing | reproduced |
                 not_reproduced | diagnosed | handed_off | fixed |
                 accepted
verdict          text (the current reading, by the dig or the
                 assessment)
assessment_ids   JSON list of corun page group ids
package          JSON: path or URL of the reproduction package, built time
updated_at, created_at
```

`rows_lost` and `events_lost` are what the bias question needs: the
crashed rows of a configuration whose output was never delivered by
any later attempt, computed from the delivery record the residual
rerun already uses (`pcs/manifests.py`, `verify`), so a configuration's
loss is stated at row level and a physics group can test whether the
missing chunks are random. A configuration is never made whole by
skipping crashed rows; the remedy is a fix and a residual rerun, which
runs the same rows with the same seeds and crashes again until the fix
is real.

### 3. Catalog page

`/panda/segfaults/`, view `panda_segfaults` in
`monitor_app/viewdir/pandamon.py` beside `panda_errors_list`, template
`monitor_app/panda_segfaults.html`, URL names `panda_segfaults` and
`panda_segfault_detail`. Linked from the PanDA hub beside Error
Summary (`panda_hub.html`) and from the production workflow hub
(`prod_hub_workflow.html`). The page renders from swfdb only; nothing
in its render path reaches the PanDA database or any remote service.

List: one row per signature, the four classes as filter chips with
counts, columns: class, signature (exit code and, at trace level, the
frame), tasks (count, with the composed names in the detail), crashes,
rate, time to death (p10/p50 minutes), sites, rows lost, status, last
seen. Default order: crashes descending. The inclusive-filter
mechanism (swf-monitor `docs/INCLUSIVE_FILTER.md`) applies if it is
the house filter by then; otherwise the error summary's chips.

Detail `/panda/segfaults/<key>/`: the signature's tasks with links to
the task pages, the configuration block, the site table, the crashed
jobs (paged, linked to job pages, with row, host, minutes, maxrss),
the trace block with its source job, the reproduction table with the
canary task links, the verdict, the assessments, and the reproduction
package link. Actions on the detail page: Dig (fetch the trace of a
chosen representative), Reproduce (choose a job, a queue), Diagnose
(submit the codoc study), each queued to the production operations
agent and reported back over the SSE relay in the pattern of the
compose-page operations (EPICPROD_OPS_AGENT.md). Actions are
authenticated operator decisions under the authority gate.

Job page: a crashed job's page shows a Crash card naming the
signature and linking to it.

MCP: `panda_segfault_catalog(status=None, class_hint=None, limit=50)`
and `panda_segfault_signature(key)` in `monitor_app/mcp/pandamon.py`,
returning the list and detail as the page shows them, so a bot or an
assessment can read the catalog. REST: `/api/segfaults/` and
`/api/segfaults/<key>/`, same payloads, for out-of-process consumers.

Acceptance: the page renders on the external face
(`https://epic-devcloud.org/prod/panda/segfaults/`) with the campaign's
signatures in their classes; the detail of the largest storm entry
lists its nine tasks; the job page of a representative crashed job
carries the card.

### 4. The dig: traces from the record

Traces are not in the record for the campaign to date: on a crash the
ERR trap exits `run.sh` before the Logs stage, so the stage logs never
upload; what exists is the pilot's log tarball with `payload.stdout`,
which carries the last 1000 lines of the crashing stage's output
(`run.sh` tees each stage through `tail -n1000`), and that is where a
Geant4 or ROOT backtrace lands when the program prints one. The
tarball is registered in the task's log dataset on BNL_PROD_DISK_1 and
the existing doer fetches and caches it
(`scripts/cache-payload-log.py`, `fetch_payload_log`,
`$SWF_TMP_DIR/panda-logs/<jeditaskid>/<pandaid>/`). The implementer
confirms on one representative job of each class that the tarball
exists for a job that exited 139 and where the backtrace appears
before building on it.

The dig is bounded as in ERROR_ATTRIBUTION.md: one representative job
per record-level signature, chosen as the job with the median time to
death, fetched through the doer; then `trace_extract` in
`monitor_app/epicprod_inventory.py` beside `diagnosis_from_log_texts`
reads the cached members for the backtrace block (Geant4's
`*** Break *** segmentation violation` and the frame list that
follows; ROOT's `Stack trace`; glibc's `Segmentation fault` line) and
records program, stage (from the stage log if present, else from which
stage's output the block sits in), and the top frames, bounded to
twenty lines, on the signature with `trace_status = found`; a fetched
log without a block records `absent`, a fetch that fails records
`log_unavailable` with the doer's reason. Two representatives with the
same top frame merge their record-level entries into one trace-level
entry; the record-level entries remain as members.

The dig runs on operator request from the detail page and once,
automatically, for every new signature the nightly inventory creates,
capped at ten fetches per night so a storm does not turn into a
thousand log reads.

### 5. Reproduction

A reproduction is the crashed row run again through the production
payload as a one-job canary task, which is the payload canary with a
chosen row instead of row 1:

- `scripts/submit-evgen-task.py`: a `--canary-row N` option (1-based
  manifest row) beside `--canary-stamp`; the canary block selects
  `spec['csvRows'][N-1]` instead of `[:1]` and the log line names it.
- `evgen_job_dispatcher.py`, `run_payload_canary`: takes the row to
  run; the canary's environment carries `CANARY_ROW`.
- site-canary CLI `canary payload-canary --task NAME --queue Q --row N`
  and the `payload_canary` message the canary agent handles gain the
  row; the `ProbeRun` records it.
- For a legacy attempt the row is the one resolved on the
  `EpicProdJob` row; a legacy task is first moved to PCS (EPICPROD_RETRIES.md,
  Move this task to PCS) so a task identity and configuration exist to
  submit under.

Each reproduction is two runs: the production queue where the crash
happened and the reference queue, which is a queue of known
conditions: the npps0 test queue `BNL_NPPS_GPU`
([NPPS0_TEST_QUEUE.md](NPPS0_TEST_QUEUE.md)), where the node, its
memory, the image and the container runtime are known and the host can
be examined directly (core dumps, a debugger on the crashed process).
Outcomes recorded on the signature: `reproduced`
(both crashed), `site_dependent` (production only), `not_reproduced`
(neither), `inconclusive` (a run failed for another reason, stated).
The canary verdict machinery reads the payload report at the next
collection cycle; the reproduction reads the same report plus the exit
code and, for a crash, fetches the trace through the dig, so a
reproduced crash yields its trace even where the campaign's did not.

Memory: a crash that only appears near a queue's memory ceiling is a
platform condition, and a fat reference node hides it. The canary
environment carries `CANARY_MEM_LIMIT_MB`, honored by the dispatcher as
a `RLIMIT_AS` on the payload process, set from the production queue's
`maxrss` limit for the reference run, so the two runs differ in site
and not in memory.

Narrowing to the event: the per-event seed under `skipNEvents` is not
established here (Geant4's event counter may restart at zero after a
skip and change the per-event seed), so the reproduction runs the whole
row. An implementer who verifies the seeding on one reproduced crash
may add `--canary-events K --canary-skip S` to run one event; until
then the row is the unit.

**The reproduction package** for a software expert:
`scripts/segfault-repro-package.py <pandaid>` writes a directory (and
tarball) with a README stating the crash, the signature and the
outcome of the two canary runs; `run-repro.sh`, which runs the campaign
image through `apptainer` (or `eic-shell`) with the payload from the
release, the manifest row, the environment file and the seed, streaming
the input from the JLab door by path exactly as the job did; the trace
as found; and the job records of the original and the reproductions.
The package contains no credential: the input path is public read and
the run registers nothing (`CANARY_OUTPUT_DATASET` unset and upload
off). It is written under `$SWF_TMP_DIR/segfault-packages/<key>/` and
its path recorded on the signature; the detail page serves it.

### 6. Diagnosis

The LLM part is a codoc study, on the assessment harness pattern:
deterministic evidence in, structured judgment out, registered as an
assessment on the signature.

- Front end `scripts/segfault-diagnosis-trigger.py`: assembles the
  bundle (the signature record, the trace, the job records of the
  representative and the reproductions, the configuration with image
  and detector version, the package README), stores it as a hidden
  corun bundle Page in section `epicprod.segfault`, and submits the
  run with the two-POST contract (prompt, then job with definition
  `segfault_diagnosis`). The run holds the SWF MCP toolset, LXR (the
  EIC code index, for cross-referencing frames to source) and the
  read-only GitHub service (issues and commits in `eic/epic`,
  `eic/EICrecon`, `eic/npsim`, DD4hep and Geant4 where indexed).
- The definition's task, in the system prompt: name the crashing
  component and function; state whether the frame matches a known
  issue or a fix between the campaign image and a later one; classify
  the crash as software defect, configuration (a geometry or beam
  setting the software does not support), event-shaped (a class of
  input the software mishandles), or platform (memory, node); state
  what a production operator should do: bump the image, change the
  configuration, hold the configuration, or hand off; and produce the
  expert handoff text when handing off.
- Schema: `{component, function, stage, classification, known_issue
  {url, fixed_in}, operator_action, confidence, handoff_text,
  generation_report}`; the harness validates it, one bounded repair
  run on mismatch, quarantine on a second failure, as in
  EPICPROD_ASSESSMENTS_V1.md.
- Completion: the existing corun callback reaches swf-monitor; the
  handler registers `epic_register_ai_assessment(subject_type=
  'crash_signature', subject_key=<key>)`, a new subject type in the
  table in EPICPROD_OPS.md § AI assessments, sets the signature's
  status to `diagnosed` and its verdict to the classification and
  operator action, and buffers one Capcom notice (source
  `swf-segfault-diagnosis`) at severity warning for a software defect
  or configuration finding and info otherwise.
- Verdict floor: a `configuration_dead` signature cannot be classified
  below configuration; a `reproduced` signature cannot be classified
  platform.

Diagnosis runs on operator request from the detail page and
automatically once a signature reaches `reproduced` with a trace.

### 7. Handoff

A signature the diagnosis classifies as a software defect, or that it
cannot classify, is handed off: status `handed_off`, the package and
the handoff text on the signature, and a notice on the production
notice stream. The handoff is the package plus a short statement: the
image, the row, the command, the trace, how often it happens and
where, and the reproduction outcome. The signature stays open until a
fix is named (`fixed`, with the fixing image or commit recorded) or
the production side accepts the loss (`accepted`, with the loss
stated); the residual rerun of the affected configurations under the
fixed image is the closing action, recorded on the signature by the
rerun's `PandaTasks` rows.

### 8. Traces going forward: the payload change

For jobs from now on, the trace should not depend on the pilot's
1000-line window. Two changes to `swf_epicprod/payload/run.sh`, both
small:

- The ERR trap, before exiting on a crash-class status, writes the
  last 200 lines of the failing stage's log into the payload report's
  note and sends the report through the report channel
  (`payload/report_out.py`, JOB_REPORTING.md), inside the per-job
  message cap. The digest in the job metrics already names the stage;
  the report now carries the trace.
- The trap then runs the log upload block, so the stage logs and prmon
  outputs of a crashed job reach `LOG_RSE` as a finished job's do. A
  failed upload does not change the exit code.

With these, the nightly dig reads the trace from the report store
(the sweep of JOB_REPORTING.md files it beside the job) and fetches no
tarball. The exit-code registry gains the crash-class row.

## Production disposition

The catalog feeds two consumers with two different questions.

Production operations asks, per configuration, whether to proceed. The
rule: a configuration is not made whole by skipping crashed rows,
because crashes may select events of a particular shape and skipping
biases the sample. The catalog states the loss per configuration at
row and event level; the production decision is fix and rerun the
residual, hold the configuration, or accept the stated loss on the
record. A configuration crashing at volume is the task breaker of
CONTINUOUS_PRODUCTION.md § The tripwire; the inventory's per-task rate
is its input.

Software experts ask what is broken. They receive the package and the
handoff text, and nothing they have to assemble themselves.

## Sequencing

Each stage is a functional delivery, deployed and checked before the
next. Files named are the files to touch.

1. **Inventory and back-fill.** `scripts/segfault-inventory.py`; the
   `EpicProdJob` rows; the action definition; the campaign back-fill
   run standalone from 2026-07-01. Check: counts against the appendix
   SQL; a representative job's row resolved.
2. **Signature table and catalog page.** `CrashSignature` (migration on
   approval); the page, detail, nav links, job-page card, MCP tools,
   REST; the class heuristic and its SysConfig keys; the nightly chain
   step. Check: the page on the external face with the classes; the
   nightly step's action record the next morning.
3. **The dig.** `trace_extract`; the Dig action; the automatic dig for
   new signatures; trace-level merging. Check: a trace on one
   representative of each class, or its `absent` or `log_unavailable`
   status stated on the page.
4. **Reproduction.** `--canary-row` through the doer, the dispatcher,
   the site-canary CLI and agent; `CANARY_MEM_LIMIT_MB`; the Reproduce
   action; the package script. Check: one signature of each class
   reproduced or not on two queues with the outcome on the page; one
   package built and run by hand in eic-shell to the same crash.
5. **Diagnosis and handoff.** The corun section, definition and system
   prompt; the trigger; the completion handler; the assessment subject
   type; the notices; the Diagnose action and the automatic trigger.
   Check: one reproduced signature diagnosed, its assessment on the
   detail page, its notice in the feed.
6. **Payload trace capture.** The two `run.sh` changes; the registry
   row; the dig reading the report store. Check: a canary crash (a
   deliberate `kill -SEGV` of npsim in a canary run) delivers its trace
   through the report channel.

Stage 1 is independent of the rest and is the first thing to run,
because the back-fill is what shows how many variants the campaign
holds.

## Verification before reliance

Facts this plan uses that the implementer confirms on the record
before building on them, each on one representative job:

- prmon returns the wrapped program's signal status (the archive
  holds 139 on jobs whose stage log names the simulation, which is the
  expected sign; confirm on one job's payload stdout).
- The log tarball of a job that exited 139 is registered and fetchable
  through the doer, and the backtrace sits in `payload.stdout`.
- The dispatcher's sequence number is the first argument in the job
  parameters, and a legacy job's parameters carry the run script's
  argument list.
- The per-event seed under `skipNEvents` (only if event narrowing is
  attempted).

## Appendix: the inventory query

The aggregate that produced the numbers above, run read-only against
the PanDA database (`PANDA_DB_*` in the operating account's
environment), for checking the builder's totals:

```sql
WITH j AS (
  SELECT pandaid, jeditaskid, computingsite, transexitcode, jobstatus,
         modificationtime, starttime, endtime, modificationhost, maxrss
  FROM doma_panda.jobsarchived4
  WHERE modificationtime > now() - interval '65 days'
  UNION ALL
  SELECT pandaid, jeditaskid, computingsite, transexitcode, jobstatus,
         modificationtime, starttime, endtime, modificationhost, maxrss
  FROM doma_pandaarch.jobsarchived
  WHERE modificationtime > now() - interval '65 days')
SELECT jeditaskid, computingsite, transexitcode, count(*) AS n,
       percentile_cont(0.5) WITHIN GROUP
         (ORDER BY extract(epoch FROM (endtime - starttime))/60) AS med_min,
       count(DISTINCT modificationhost) AS hosts
FROM j
WHERE jobstatus = 'failed' AND transexitcode IN ('134','135','136','139')
GROUP BY 1, 2, 3 ORDER BY n DESC;
```
