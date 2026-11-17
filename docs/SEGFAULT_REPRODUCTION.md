# Segfault reproduction and diagnosis

How a crash signature of the catalog ([SEGFAULT_DIAGNOSIS.md](SEGFAULT_DIAGNOSIS.md))
gets its trace, its reproduction, its diagnosis and its handoff, and how
the payload captures a crash's trace as it happens. The sections keep
their numbers from the catalog document, where 1 to 3 (inventory,
signature record, catalog page) remain. The kinds beyond segfaults are in
[SEGFAULT_KINDS.md](SEGFAULT_KINDS.md).

## Components

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

A job at a cache-stdout queue (BNL_ePIC_GOOGLE) registers no log
tarball; what it leaves is the harvester's stdout in the PanDA cache,
which the record copies hourly before the cache's seven-day purge
(EPICPROD_OPS.md, Harvester stdout records), and the dig reads that
copy as the job's log when no tarball exists.

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

As built (2026-09-11): `trace_extract` reads three forms. eicrecon
prints JANA2's backtrace into `payload.stdout` (`[fatal] Segfault
detected!`, `Backtrace:`, numbered frames each with its library and
offset), and the crashing frame is the first named one: job 994773
(task 37333, beam gas) dies in `podio::ROOTWriter::resetBranches`,
libpodioRootIO.so, at the first output write; job 2723039 (task 39623)
in `eicrecon::PulseCombiner::sumPulses`, libalgorithms_digi.so, at
event 358. npsim prints no backtrace: DD4hep's handler reports
`[FATAL] (SignalHandler) Handle signal: 11 [SIGSEGV]`, and the
evidence is the last `G4Exception` block before it, which names the
exception, the Geant4 function and the volume; the extractor makes
that the frame (job 2723082, task 39623: `G4Exception GeomNav0003 in
G4Navigator::ComputeStep`, a stuck optical photon in `DRICH_gas_0`
after a geometry-overlap warning at `DRICH_mirror_sec2_425`), whether
the signal is 11 or, when glibc aborts the process on heap corruption
after the event abort, 6 (jobs 2723594 and 2723599; ROOT's frameless
`*** Break *** abort` banner follows the signal and decides nothing).
The Geant4 and ROOT `*** Break ***` forms with gdb-style frames are
read too. The stage comes from the prmon error line (`${TASKNAME}.<stage>.prmon`)
or from the payload digest.

Task 39623 shows that a record-level signature (exit code by task) can
hold two crashes: five reconstruction deaths and two simulation
deaths. Since 2026-09-15 a task whose crashed jobs carry two or more
stages (the payload digest names the stage; every new-payload job
carries one, no legacy job does) splits by stage in the record pass:
one entry per stage with the stage on the key
(`exit139:task39623:simu`, `:reco`), the crashes of no recorded stage
under the plain key, and a plain entry left with no crash retired,
its trace, reproductions, verdict and marking handed to the stage
entry they concern and its name in the findings' signature lists
replaced by the stage entries'. A stage entry's jobs, representative
and reproductions are its own stage's. A legacy task's two crashes
cannot be told apart by the record; the reproduction tells them: a
member whose reproduction settled it under another finding than the
frame's (task 38864: the trace of one row's reconstruction crash,
finding f-4; the reproduced rows the dRICH simulation crash, f-3)
leaves the frame at the next merge, its crashes no longer counted
there, its page saying so; the frame entries are recomputed from
their members after every record pass (`remerge_frames`). The Dig
control still accepts a chosen job for the other stage. The log tarballs
of the storm, sparse and mixed representatives at BNL_OSG_EPIC_PROD_1
have no replica in BNL Rucio (jobs 1801822, 2249415, 1951234); those
signatures carry `log_unavailable` with the reason, and their cause
comes from reproduction. The tarballs of the Perlmutter and
BNL_OSG_PanDA_1 jobs are present. The tarballs of the legacy tasks
whose log RSE was JLab (EIC-XRD-LOG, 27 signatures on 2026-09-13) are
catalogued by the JLab WebDAV door (`davs://dtn-rucio.jlab.org:1094/...`),
which xrdcp cannot read; the same door serves xrootd on the same port
and admits the JLab production proxy (`EVGEN_X509_PROXY`), so the
fetch doer (`scripts/cache-payload-log.py`) reads such a PFN as
`root://` with that proxy, and those digs land (task 38970's on
2026-09-13). `study_job` reads the log DID from
`filestable4`, which is purged after about thirty days, so the job
page's payload-log fetch cannot start for an older job; the dig
resolves the DID through the task's log dataset contents row, whose
LFN carries a `$JEDITASKID` placeholder, scope `group.EIC`.
`scripts/segfault-dig.py` (`--key`, `--pandaid`, `--auto N`) is the
doer; the ops agent runs it for the Dig action (`segfault_dig` message,
completion event `segfault_dig_done` on the SSE relay) and as the
`segfault_dig` chain step after the inventory.

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
happened and a second queue. When the queue record has the crash's queue
closed to new work, the production run goes to the open queue that shares
its slots and runs with the production role (`production_stand_in`; since
2026-11-11, a crash at `BNL_OSG_EPIC_PROD_1` reproduces on
`BNL_OSG_PanDA_1`, where before its submission was refused). Since 2026-09-13 the second queue is by
default a second production-class queue (`UM_GREX_PanDA_1`, or
`BNL_OSG_PanDA_1` for a crash at GREX, since 2026-10-22 when
`BNL_OSG_EPIC_PROD_1` left production; `elsewhere_queue` in
`monitor_app/segfaults.py`), because the crash-or-not verdict and the
trace come from any queue once the payload carries the crash capture
(§ 8), and the many slots of a production queue return the verdict in
hours where the reference queue's one slot took days. The reference
queue, the npps0 test queue `BNL_NPPS_GPU`
([NPPS0_TEST_QUEUE.md](NPPS0_TEST_QUEUE.md)), is a queue of known
conditions, where the node, its memory, the image and the container
runtime are known and the host can be examined directly (core dumps, a
debugger on the crashed process, a stall watched live), and is asked for
when the reading needs that: the page offers it beside the pair, with
the production queue's memory limit. A run there asks for the node's
eight cores, with the job's memory request and the payload's threads
unchanged, because the pilot allows a single-core job on a multi-core
queue only its share of the work directory, about 4.7 GB on npps0
(`WHOLE_NODE_CANARY_QUEUES` in swf-monitor
`scripts/submit-evgen-task.py`; since 2026-11-13). Each request records its run's
role (`production`, `elsewhere`, `reference`); the production run
against the other run settles the outcome, whichever the other is.
Outcomes recorded on the signature: `reproduced`
(both crashed), `site_dependent` (production only), `not_reproduced`
(neither), `inconclusive` (a run failed for another reason, stated).
The canary verdict machinery reads the payload report at the next
collection cycle; the reproduction reads the same report plus the exit
code and, for a crash, fetches the trace through the dig, so a
reproduced crash yields its trace even where the campaign's did not.

The image is the crashed job's, with one exception. The older tasks
name their image by the ePIC repository's cvmfs path
(`/cvmfs/eic.opensciencegrid.org/singularity/eicweb/...`), which the OSG
pilots do not serve; they serve OSG's copy of the same tag
(`/cvmfs/singularity.opensciencegrid.org/eicweb/...`). A run at a queue
whose pilots refuse the job's path and serve the copy runs the copy,
and its request records the path it replaces (`container_swapped_from`;
`run_image` in swf-monitor `monitor_app/segfaults.py`). This holds for
reproductions only, where the run asks whether the row fails again at
that queue, and the reference queue runs the job's own path (Torre
Wenaus, 2026-11-13). Before it, each such production run was refused at
submission.

As built (2026-09-13): the reconciliation reads the trace of a
reproduced crash from the crashed run's payload report when the
signature has none, the reference run first (`report_note` and
`trace_from_report` in swf-monitor `monitor_app/segfaults.py`; a job
that finished at the server has its report in the PanDA metatable, a
swept job in its filing), records and merges it as a dig does, and the
dig's `--pandaid` accepts one of the signature's reproduction jobs for
the same read by hand. A reproduction run before payload 0.12 carries
no crash note, and its trace comes from the log tarball or not at all
(38970's runs of 2026-09-11). Once the trace and the outcome are both
on record, a signature whose frame a finding already names is marked
`diagnosed` by that finding (verdict "covered by finding f-N", the
diagnosis record `covered`) and no study is queued: the finding is the
reading, and nothing further is asked of the signature. The automatic
study fires only for a frame with no finding.

A task sent to the reference queue carries no log dataset: its pilot
log goes to the devcloud S3 store, which the PanDA server cannot
register, and the adder fails a job on that whatever the payload did
(every reproduction of 2026-09-11, DDM 200). The submit doer sets
`noLog` for `BNL_NPPS_GPU` (`OBJECT_STORE_LOG_QUEUES` in swf-monitor
`scripts/submit-evgen-task.py`); the crash tail then reaches the
record through the report channel (payload 0.12.0) while the pilot log
stays on the host. A payload canary whose job failed after the payload
ran to its exit (a stage-out or registration failure, no executor or
pilot error) is collected from the job digest, exit code and stage, so
a verdict never waits on the pilot's stage-out; and the report sweep
reads every canary job's report whatever its signature's count.

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

**The nightly notice.** Nothing submits a reproduction on its own: a
reproduction is a request on the record, and the decision to make one
is a person's or an LLM session's. So the catalog says what it holds
that awaits a decision. The last segfault step of the nightly
`catalog_sync` chain (`segfault_notice`, after the inventory and the
automatic dig; `scripts/segfault-nightly-notice.py`) sends one TJAI
peer message to the swf sessions on the monitor host (the
`host:swf-testbed` group, from a registered sender of its own,
`epicprod-nightly`) with the census of the catalog (signatures and
crashes; read by a finding; a reproduction in flight; no trace and a
runnable row; traced with no reading; settled by a reproduction with
no trace and no reading; no trace and no runnable row) and three
lists, largest first: the signatures for which reproduction is the
only extraction left (no trace, a runnable row, no attempt in flight,
no finding), the traced signatures no finding reads, and the
signatures whose reproduction has settled without a trace (the row ran
clean at both queues, or crashed without a readable trace) that no
finding reads: those are read from the record, the per-host and
per-day shape of their crashes, into a finding
(`panda_segfault_finding_set`), never run again. A session takes a
reproduction item by requesting it on the record,
with the MCP tool `panda_segfault_reproduce(key)` (the production
queue and the reference queue, the crashed row of the representative;
under the authority gate) or the signature page's Reproduce; the
request shows on the runs page within a minute, which is what keeps
two sessions from submitting the same row: read the signature's
attempts before requesting. The action `segfault_notice` carries the
counts and the message id; a send that fails is an error on the
stream, never a silent night.

**Visibility of the attempts.** Two records make a reproduction
attempt: the request on the signature (`CrashSignature.reproduction`,
one entry per queue, with a `request_id` the canary message carries)
and the execution in the canary store (the `ProbeRun` of kind payload,
tagged with the signature, the crashed job and the request id). One
read-only summary joins them (swf-monitor
`monitor_app/reproductions.py`), one row per attempt, exact by request
id and, for entries from before the id existed, by queue, crashed job
and request time, one to one and never reassigned; a run whose request
mirror is stale is still a row. The summary feeds the catalog's
Reproductions column (active and ended attempts per row, a trace-level
row counting its members' attempts, each attempt once), the runs page
`/panda/segfaults/reproductions/` (every attempt under the narrowing
filter by execution, result, queue, role and signature, the active
ones first, with each attempt's last observation), and the signature
page. Execution and result are stated apart. Execution: queued for
submission (a request without a run), submitting, queued, running,
finishing, finished, failed, cancelled, submission failed; a job's
terminal state outranks a run still flagged submitted. A request the
agent did not run is withdrawn on the record by the agent itself, with
the reason, through the reconcile script's `--withdraw`: a duplicate the
agent's dedup dropped (two sessions asking for the same row and queue
in the same minute, 2026-09-15) reads cancelled, a dispatch that failed
before a run existed reads submission failed; either counts for nothing
in the pair and closes the request, so no request reads queued for
submission forever or keeps its signature open to every pass. Nothing is
withdrawn when a run carries the request id: the run is the record. Result: pending,
awaiting report (the job ended, the report not yet collected), crash
reproduced (the payload's exit in the crash class), completed without
crash (exit 0), inconclusive with the reason; a finished PanDA job is
not a clean payload, since the canary wrapper exits 0 to keep the
report, and a failed one is not a crash. Waiting time stops at the
start, running time at the end; a running attempt shows its time so
far. A page read reads the two stores and writes nothing: the join is
written back onto the signature (each entry's run, task, job and
outcome; the pair's settlement; the diagnosis trigger, once) by the
canary agent after every probe collection
(`scripts/segfault-reproductions-reconcile.py`, the
`segfault_reproduction_reconcile` action when something changed),
under a row lock so a request recorded meanwhile is kept. A request posts
nothing to epicprod-live; a reproduction that settles posts one record,
`segfault_reproduction_settled`, with its outcome and the finding that
reads it or the study queued (`reproductions.log_settled`; Torre Wenaus,
2026-11-03). Collection
runs hourly with the probe tick and every five minutes as a
collect-only tick while runs are open (site-canary
SWF_INTEGRATION.md), so an attempt's state on the pages is at most
minutes old while it runs; the pages show when PanDA was last read for
each attempt. A request whose second queue's send fails keeps its first
queue's request on record.

**Local reproduction** on the detail page: the representative job's run
as a shell fragment to copy, for a software expert with the image at
hand: the image, the payload environment of the task with registration
and the copies off, and the payload's invocation with the row's
arguments as the dispatcher passes them (`run.sh EVGEN/<file> <ext>
<events> <chunk>`), with the input's public path, the seed and the
skip stated (`local_repro` in `monitor_app/segfaults.py`, from the
same environment builder the package uses). The clone is pinned to the
payload the job ran: the attempt's submission record carries the
payload version, each version was set by one commit (the one that
wrote it into `payload/VERSION`), and the fragment checks that commit
out by `git log -S` on the clone, so no tag or table is kept. An
attempt without a version is a legacy attempt that ran the production
team's script; the block says so and runs the current payload over the
same row. A job whose row is unresolved or whose task is not in PCS
gets the reason instead.

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

Diagnosis runs on operator request from the detail page,
automatically once a signature reaches `reproduced` with a trace, and,
since 2026-09-13, nightly for the traced signatures no finding reads
without waiting for a reproduction: the `segfault_study` chain step
(between the dig and the notice, `scripts/segfault-study-auto.py`)
queues the study of such signatures largest first, at most
`EPICPROD_SEGFAULT_STUDY_AUTO` (3) a night, each marked queued before
its message goes out so no night asks twice; a traced signature whose
frame a finding already reads is marked `diagnosed`, "covered by
finding f-N", the same night and outside the cap, since its reading
exists and nothing further is owed. Reproduction and diagnosis are the
task's ordinary moves and need no word beyond the record.

As built (2026-09-11): the contract is `swf_epicprod/segfault/spec.py`
(section `epicprod.segfault`, bundle section `epicprod.segfault.bundle`,
definition `segfault_diagnosis`, the system prompt, the artifact schema
with the verdict floor, the report renderer);
`swf_epicprod.segfault.bootstrap` creates the sections, the versioned
system prompt and the definition in corun-ai over REST on the assessment
bootstrap's client (Codex Sol at xhigh, thirty-minute worker timeout,
the tools tjai, swf-testbed, xrootd, lxr, github-readonly) and prints
`CORUN_SEGFAULT_DEFINITION` for `production.env`. The front end is
`scripts/segfault-diagnosis-trigger.py` (the bundle from the signature
record, its trace, the representative and reproduction job records and
the package README; the hidden bundle page; the two-POST submission; the
run recorded on the signature under `data['diagnosis']`; the action
`segfault_diagnosis_triggered`). The corun completion callback routes a
`segfault_diagnosis` run to the ops agent as
`segfault_diagnosis_completed`, which runs
`scripts/segfault-diagnosis-enforce.py`: extract, validate against the
schema and the floor, one repair run, quarantine on the second failure;
a valid artifact is rendered with the bundle's facts, registered as an
assessment on the signature (subject type `crash_signature`, resolver in
`swf_epicprod/ai_subjects.py`), the status becomes `diagnosed`
(`handed_off` for a hand-off, the handoff text kept on the signature's
package record) and the verdict the classification and action. The
`segfault_diagnosis` action carries the classification as its severity
(warning for a software defect or a configuration finding, info
otherwise), the narration and the signature URL, which is what notice
routing (swf-monitor NOTICE_ROUTING.md) delivers to a subscriber; a
Capcom subscription to `segfault_diagnosis` is the feed. The page's
Diagnose control queues `segfault_diagnose` to the agent and hears
`segfault_diagnose_queued` and `segfault_diagnosis_done`; the automatic
trigger fires in the canary agent's reconciliation after a collection
when the outcome stands as reproduced or site_dependent with a trace
on record and no finding names the frame, once (the signature's
diagnosis record is marked queued before the message goes out, so a
later pass never queues it twice; a frame with a finding is marked
covered instead, § 5).

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

### Fatal signals followed by a stall

A terminal job status and a payload failure are separate observations.
Reproduction job 2723541 (task 39792, original job 2236724/task 38864)
ended at the Perlmutter wall-time limit, but its preserved npsim log
records GeomNav0003 at the DRICH gas/mirror boundary, heap corruption
(`corrupted double-linked list`) and SIGABRT. The stage's CPU and file-I/O
counters were unchanged from 12:02:33 to 15:29:13 UTC on 2026-09-12.
The attempted ACTS reconstruction reproducer did not reach reconstruction.

The payload watchdog described in EPICPROD_PAYLOAD.md preserves explicit
fatal-signal evidence before termination and stops a stalled fatal stage
after 120 seconds of measured inactivity. Reproduction rows show **Crash
observed**, including the signal and stage, independently of the execution
status and actual payload exit. This evidence alone does not settle the
requested signature as reproduced: a different stage or fault may have
prevented that code from running.

The canary collector keeps the fatal digest and full-report evidence.
The shared reproduction projection also reads fatal evidence from reports
filed by the existing report sweep. It performs no remote fetch on a page
read. Historical logs can be attached with swf-monitor's
`scripts/record-reproduction-fatal.py --job ID --stage simulation --log FILE
--source URL`; it previews by default and `--apply` records the evidence
on the matching reproduction run without inventing an exit code. Run the
existing reconciliation script for the signature after a historical repair.

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

As built (2026-09-11, payload 0.12.0): the ERR trap calls
`crash_capture` on a status from 128 to 159, which puts "crash: <stage>
(<program>) exited <s> (signal N)" and the last 200 lines of the stage's
log (`npsim.log`, `eicrecon.log`, `hepmcmerger.log`, `evgen.log` by
stage) into `REPORT_NOTE`, so the report the EXIT trap writes and sends
carries the trace, and then calls `upload_logs`, the Rucio log upload
of the Logs stage made a function, when the configuration copies logs
through Rucio. A live check under the trap (a SIGSEGV in the simulation
stage) exited 139 with the tail in the note and the stage marked
failed. The dig reads a filed report whose note begins `crash:` before
resolving any tarball. The registry row is in EPICPROD_PAYLOAD.md.
