# Segfault diagnosis

Payload crashes (segmentation faults and related signal deaths) in
ePIC production jobs, found from the production record, catalogued by
signature, reproduced as standalone runs, diagnosed with LLM assistance
where the evidence allows, and handed to software experts as
self-contained reproductions where it does not. This document holds
the record, the inventory, the signature record, the catalog page, the
findings and the production disposition. The dig, reproduction,
diagnosis and handoff, and the payload's crash capture, are in
[SEGFAULT_REPRODUCTION.md](SEGFAULT_REPRODUCTION.md); the kinds beside
segfaults (memory kills, bad input, Hilog, Event Service units) are in
[SEGFAULT_KINDS.md](SEGFAULT_KINDS.md). Each component names the files
it touches and the check that shows it working.

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

**Row.** The manifest row a job ran: `file,ext,nevents,ichunk`, and a
fifth field, the start, on a file's short last chunk
(`pcs/manifests.py`; JEDI_INTEGRATION.md § Components). The in-job dispatcher runs row *n* for job
sequence number *n* (`evgen_job_dispatcher.py`, `run_row`), the
payload seeds the simulation with `ichunk + 1`
(`run.sh`, line 182), enables the per-event seed
(`--random.enableEventSeed`) and mixes the background merger's seed
from the input name and the chunk seed. A row is therefore sufficient
to rerun the job's work: the same image, input file, events, chunk and seed.

## Components

### 1. Inventory builder

`swf-monitor/scripts/segfault-inventory.py`. A script run under the
monitor's virtual environment with Django configured, reading the
PanDA database through `connections['panda']` (the connection
`monitor_app/panda/queries.py` uses) and writing to swfdb through the
ORM. It runs standalone for the campaign back-fill and as a nightly
chain step for top-ups.

Selection: jobs in `doma_panda.jobsarchived4` with `jobstatus =
'failed'` and `transexitcode IN ('134','135','136','139')`,
`modificationtime` inside the window, of production tasks only
(`jedi_tasks.processingtype = 'epicproduction'`): the testbed's
streams (fast and prompt processing, EICFast) are not production and
never enter this record (2026-09-13; eight EICFast tasks and their 85
crashed jobs were removed from it that day). That table holds the whole
campaign (its oldest rows are from September 2025) and the archive
schema `doma_pandaarch` holds no job of the period, so the live table
is read alone, as every other PanDA query in swf-monitor reads it.

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
attemptnr, row (file, ext, nevents, ichunk, start, when resolved),
stage (from the payload digest in jobmetrics when present),
trace_status (unknown | found | absent | log_unavailable)
```

The sequence number is the job's `pseudo_input` file in
`doma_panda.filestable4` (dataset `seq_number`, LFN the number), and
that table, like `jobparamstable`, is purged after about thirty days:
of the 116,334 crash-class jobs in the 65 days to 2026-09-11, 6,874
still had a file row. Beyond the purge the number comes from the
tables JEDI keeps with the task: `jedi_job_retry_history` links every
retried job to its successor, and the `seq_number` dataset's row in
`jedi_dataset_contents` carries the pandaid of the row's last attempt,
so a job's sequence number is that of the last job in its retry chain.
The chain resolves 116,245 of the 116,334 (chains run to depth 9), and
on every job where the file row also exists the two agree. The
dispatcher's first argument in the job parameters is the last resort.
The six-digit serial in a job's log file name is JEDI's job counter,
not the row (task 39623 has four rows and serials to eleven), and is
not used.

The row is `expand(record_of(panda_tasks))[seq − 1]` when the
attempt's manifest record exists (`pcs/manifests.py`), else left
unresolved and counted. The record exists on 85 of the 351 attempts
(every PCS submission; legacy attempts only where the sandbox
keepalive found the sandbox still cached), so the back-fill resolved
the row on 4,888 jobs; the storm tasks are legacy attempts whose
sandboxes were purged before the keepalive existed. Their sequence
numbers are on the record, so the reconstruction path of
`attempt_manifest` (the definition's per-file totals and the attempt's
row count) can supply the rows without a further PanDA read; that pass
is not part of stage 1.

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

Thresholds are SysConfig keys (`segfault_class_dead_rate`,
`segfault_class_storm_rate`, `segfault_class_storm_iqr`,
`segfault_class_sparse_rate`) with these defaults, seeded on first read
and visible on the System page as every SysConfig key is. The
grouping and the class reading live in `monitor_app/segfaults.py`
(`build_record_signatures`), run by the builder after its job pass
for the tasks touched, or over the whole inventory with
`--signatures`. The rate's denominator is the task's finished plus
failed jobs over its whole life, one aggregate query per pass. The
delivery lookup behind `rows_lost` reaches JLab Rucio through the
residual rerun's `_delivered_row_keys` (`pcs/commands.py`), so it
runs in the builder only, largest signatures first, at most
`--rows-lost-max-tasks` (50) per pass; a signature whose rows are
unresolved or whose task has no PCS association records why instead
of a number. The first pass over the campaign (2026-09-11) produced
146 signatures: 10 storm, 11 configuration dead, 7 mixed, 109 sparse,
9 abort. A small task classifies noisily (task 39623, 4 rows and 7
crashes, reads as a storm); the thresholds are the knob.

Invocation: `segfault-inventory.py --since 2026-07-01` for the campaign
back-fill, `--days 3` for the nightly, `--check --since <date>` for
the record's count against swfdb's rows. The nightly window overlaps
deliberately; rows are keyed on `pandaid`, so a job seen twice is one
row, and a row's other content (the payload report the sweep files,
the registrar's notes) is left as it is. The back-fill of 2026-09-11
wrote 136,616 rows (all four codes, since 2026-07-01, 132 tasks) in
97 seconds, and the check matched. Every run logs one `segfault_inventory` action to the
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
                 'exit139:task38661:simu' / ':reco' when the task's
                 crashes split by stage, 'exit139:frame:<sha>' at trace
                 level
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
Summary (`panda_hub.html`), from the production workflow hub
(`prod_hub_workflow.html`), and from the production nav's Diagnostics
menu beside Error summary. The page renders from swfdb only; nothing
in its render path reaches the PanDA database or any remote service.

List: one row per signature, columns: class, signature (exit code and,
at trace level, the frame), configuration, tasks, crashes, rate, time
to death (p10/p50 minutes), queues, hosts, rows lost, stage, status,
last seen first. Default order: last seen, newest first. The narrowing
filter of the operations views (swf-monitor `docs/INCLUSIVE_FILTER.md`
§ The narrowing filter) carries the facets class, exit code, queue,
status and level: a selection narrows to the intersection and every
bar re-counts within the selection.

Detail `/panda/segfaults/<key>/`: the signature's tasks with links to
the task pages, the configuration block, the site table, the crashed
jobs (paged, linked to job pages, with row, host, minutes, maxrss),
the trace block with its source job, the reproduction table with the
canary task links, the local reproduction block (below), the verdict,
the assessments, and the reproduction package link. Actions on the detail page: Dig (fetch the trace of a
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

In [SEGFAULT_REPRODUCTION.md](SEGFAULT_REPRODUCTION.md#4-the-dig-traces-from-the-record).

### 5. Reproduction

In [SEGFAULT_REPRODUCTION.md](SEGFAULT_REPRODUCTION.md#5-reproduction).

### 6. Diagnosis

In [SEGFAULT_REPRODUCTION.md](SEGFAULT_REPRODUCTION.md#6-diagnosis).

### 7. Handoff

In [SEGFAULT_REPRODUCTION.md](SEGFAULT_REPRODUCTION.md#7-handoff).

### Fatal signals followed by a stall

In [SEGFAULT_REPRODUCTION.md](SEGFAULT_REPRODUCTION.md#fatal-signals-followed-by-a-stall).

### 8. Traces going forward: the payload change

In [SEGFAULT_REPRODUCTION.md](SEGFAULT_REPRODUCTION.md#8-traces-going-forward-the-payload-change).

## Memory kills

In [SEGFAULT_KINDS.md](SEGFAULT_KINDS.md#memory-kills).

## Bad input

In [SEGFAULT_KINDS.md](SEGFAULT_KINDS.md#bad-input).

## Hilog

In [SEGFAULT_KINDS.md](SEGFAULT_KINDS.md#hilog).

## Event Service units

In [SEGFAULT_KINDS.md](SEGFAULT_KINDS.md#event-service-units).

## Findings

The catalog is the record; what a study concludes is a reading of it,
kept apart so the record stays programmatic and the reading stays
curated. The findings live in swf-monitor's entry store (the tjai-style
`entry` table the alarm system brought in: context `segfault`, kind
`finding`, one row per crashing frame named by its catalog key, the
reading as the content, the structured fields in `data`: serial (the
permanent id, drawn once at creation and shown as f-8; the page lists
findings newest first by it), date, frame,
stage, signatures, sources, class, action, standing, fix, notes,
model_reading, and where in the detector the finding lies:
detector_components, DD4hep top-level detector names from the epic
compact files, each optionally with a volume path below it
(`BeamPipeB0/v_pipe_7`); detector_point, a point in DD4hep global
coordinates in millimetres, an unknown axis null; detector_geometry,
the geometry those refer to), which versions every substantive change with the
editor's name (`entry_version`). The production operator writes them
after checking each claim against its source, through the MCP tool
`panda_segfault_finding_set` or `POST /api/segfaults/findings/`; each
write is a `segfault_finding_set` action. A reading added to an existing
finding (a paragraph, a notes line, signatures and sources, one sentence
corrected where it stands) goes through swf-monitor
`scripts/segfault-finding-append.py`, which sends only the change. An LLM study's reading that
has not been checked is written with `model_reading` set and shown as
such. The Segfault findings page (`/panda/segfaults/findings/`) renders
them as one table joined live to the catalog for the crashes, tasks and
loss, summed over the signatures a finding names with each crash counted
once (a named frame entry carries its members' sums, so a member named
beside it adds nothing); the catalog and each signature page link to the frame's row;
`panda_segfault_findings` and `GET /api/segfaults/findings/` serve the
same. The diagnosis machinery never writes a finding on its own; a
sweep of registered diagnoses into model-reading rows is possible
later, since the artifact carries the row's fields.

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

The production lead runs the system and reviews segfault status as
clear statements and action proposals: what is established, what was
decided, and the open questions, each with a recommended answer that a
yes settles. Any question that brings the day's reading to a more
complete and higher-quality close is legitimate to put to him, from a
production decision (accept the stated loss, rerun the residual) to a
reading he should confirm, a reproduction worth running, or a finding
worth writing; the one thing never put to him is acting on the
handoffs. The resolutions are brought to the review, not extracted by
asking. What the system produces beyond that is actionable handoffs,
and they live in the findings: the issue draft with its row, image and
command, the note to a site, complete and ready for the software
experts and the sites to pick up from the findings page. Nothing is
queued on the production lead, and no machinery offers to file an
issue or write to a site (Torre, 2026-09-13).

Two cases exceed the routine and call for a top model, selectively: a
new crashing frame that no finding reads (the nightly study yields a
model reading; the finding is written only after its claims are
checked against the source, and the frame's issue draft with it), and
a crash cluster with no trace to read, whose reading comes from the
per-host and per-day record (a node event, a site condition) or from
reproduction. An LLM handling the nightly notice or a status review
names such a case when it sees one and recommends the examination
rather than reading it with a lesser model; everything else in the
process is routine.

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
- Confirmed 2026-09-11 (stage 1): the dispatcher's sequence number is
  the first argument in the job parameters (job 2723039, task 39623),
  and the same number is the job's `pseudo_input` file and the row of
  the last job in its retry chain. A storm job (1768411, task 38661)
  has no parameters and no file rows left, so the legacy argument form
  was not confirmed and the builder does not depend on it. The payload
  digest in the job metrics names the crashing stage on payload 0.11
  jobs (`payloadStage=reconstruction payloadExit=139` on 2723039) and
  is absent on the campaign's earlier jobs.
- The per-event seed under `skipNEvents` (only if event narrowing is
  attempted).

## Appendix: the inventory query

The aggregate that produced the numbers above, run read-only against
the PanDA database (`PANDA_DB_*` in the operating account's
environment), for checking the builder's totals. The archive schema
contributed no row to the 65-day window when it was run with a union
over `doma_pandaarch.jobsarchived`; the live table alone gives the
same result.

```sql
SELECT jeditaskid, computingsite, transexitcode, count(*) AS n,
       percentile_cont(0.5) WITHIN GROUP
         (ORDER BY extract(epoch FROM (endtime - starttime))/60) AS med_min,
       count(DISTINCT modificationhost) AS hosts
FROM doma_panda.jobsarchived4
WHERE modificationtime > now() - interval '65 days'
  AND jobstatus = 'failed' AND transexitcode IN ('134','135','136','139')
GROUP BY 1, 2, 3 ORDER BY n DESC;
```

The sequence number of every crash-class job beyond the purge of
`filestable4`, through the retry chain:

```sql
WITH RECURSIVE crashed AS (
  SELECT pandaid, jeditaskid FROM doma_panda.jobsarchived4
  WHERE jobstatus = 'failed' AND transexitcode IN ('134','135','136','139')
    AND modificationtime > now() - interval '65 days'),
chain AS (
  SELECT pandaid AS origin, jeditaskid, pandaid AS cur, 0 AS depth FROM crashed
  UNION ALL
  SELECT ch.origin, ch.jeditaskid, h.newpandaid, ch.depth + 1
  FROM chain ch JOIN doma_panda.jedi_job_retry_history h
    ON h.jeditaskid = ch.jeditaskid AND h.oldpandaid = ch.cur
   AND h.relationtype = 'retry'
  WHERE ch.depth < 50),
last AS (
  SELECT DISTINCT ON (origin) origin, jeditaskid, cur
  FROM chain ORDER BY origin, depth DESC)
SELECT l.origin AS pandaid, dc.lfn::int AS seq
FROM last l
JOIN doma_panda.jedi_datasets d
  ON d.jeditaskid = l.jeditaskid AND d.type = 'pseudo_input'
 AND d.datasetname = 'seq_number'
JOIN doma_panda.jedi_dataset_contents dc
  ON dc.datasetid = d.datasetid AND dc.jeditaskid = d.jeditaskid
 AND dc.pandaid = l.cur;
```
