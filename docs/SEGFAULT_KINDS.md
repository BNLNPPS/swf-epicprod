# Crash catalog kinds beyond segfaults

The crash catalog ([SEGFAULT_DIAGNOSIS.md](SEGFAULT_DIAGNOSIS.md)) holds
four kinds besides the segfault: jobs killed for their memory (Himem),
stages that stopped on an error naming their input (Bad input), jobs whose
stage logs ran over a threshold (Hilog), and Event Service units that
crashed inside a finished job. Each kind's selection, signature,
representatives, nightly pass and reproduction are below; the dig,
reproduction and diagnosis they share with segfaults are in
[SEGFAULT_REPRODUCTION.md](SEGFAULT_REPRODUCTION.md).

## Memory kills

A job that exceeds its memory does not crash; the site removes it.
The kernel's memory cgroup kills the largest process (pilot error 1212,
"Memory cgroup out of memory", payload exit 137), or the batch system
evicts the job for exceeding its memory request (pilot error 1201, "Job
killed by signal: SIGTERM", on OSG glideins). The record of such a job
holds no trace. It holds the job's peak memory as the pilot measured it
(`maxrss`, `maxpss`), its request (`minramcount`), and the stage the
payload digest names. What a memory signature establishes is how much
memory a configuration's rows need, and whether the need grows through
the row or is reached early and held.

**Selection.** A failed job of a production task is a memory kill when
its pilot error is 1212 with "out of memory" in the diagnostic and its
payload exit is 137; or 1201
with "SIGTERM" in the diagnostic and a peak resident memory at or above
`segfault_memory_rss_fraction` (SysConfig, 0.9) of its request; or its
payload exit is 137 under another pilot label with the same peak
condition. A SIGTERM or SIGKILL below that fraction is another kind of
removal (wall time, preemption) and is not counted. Over the 65 days to
2026-10-19 the rule selects about 181,500 jobs in eleven tasks, nearly
all of them in the three eAu BeAGLE tasks of 26.07.3.

The exit condition on 1212 dates from 2026-11-12. The pilot's kernel-log
scan matched an out-of-memory line to the job by process id alone, so a
job whose payload failed for another reason could be reported as killed
for a kill logged hours earlier for another job, once the node had reused
the id (finding f-28; the pilot fix is PanDAWMS/pilot3#232). Every 1212 of
a real kill on record carries exit 137, about 76,700 jobs; the 76 others
carry 1, 78, 79 or 139 and are not counted.

**Signature.** One signature per task and stage: key
`memory:task40855:simu`, class `memory`, split by stage under the rule
of the record-level crash entries, with the kills of no recorded stage
under the plain key `memory:task40855`. `exit_code` and `signal` carry
the conventional status of the dominant removal, 137 and 9 for a cgroup
kill, 143 and 15 for an eviction. The rest of the row is the record
across the task's whole life, computed by aggregate queries on the
PanDA record with no row per job: kills, the rate over finished plus
failed, time to kill (10th and 50th percentile), the peak resident
memory (median and maximum), and per site the kills, hosts, removal
mechanism and request. The back-fill from 2026-07-01 gave 42 signatures
over 37 tasks, a few kilobytes each.

**Representatives.** Each signature keeps three jobs as `EpicProdJob`
rows with `phase = 'payload_memory'`, in the shape of a crashed job's
row: among the kills whose manifest row resolves, the shortest, median
and longest time to kill. They are chosen once and kept, so a
reproduction requested against one stays valid; the back-fill wrote 77,
a signature whose kills have no resolvable row having none. The job page's card, the local reproduction and the package
read them as they read a crashed job.

**The nightly pass.** The inventory's memory pass
(`scripts/segfault-inventory.py`, after the crash pass) finds the tasks
with a memory kill in its window, recomputes those tasks' signatures
and those of every task already holding a memory signature over their
life, and picks representatives for a signature that has none. A
signature the selection no longer yields is retired unless a
reproduction is recorded on it, so a change to the rule reaches the
record at the next pass. Its `segfault_inventory` action carries the tasks and signatures
touched. The automatic dig and the automatic study pass memory
signatures by: there is no trace to read, and the diagnosis contract
reads a crashing frame.

**Reproduction.** The representative's row runs twice, as for a crash,
and is measured rather than watched for a signal:

- on the production queue where the kill happened, at the task's own
  memory request and with no address-space limit, so the site's
  enforcement repeats or does not;
- on the reference queue with no limit, where the row runs to its end
  and the payload report gives the stage's peak resident, proportional
  and virtual memory and the trend of its proportional memory over the
  stage: growth in kB per second, start, peak, and the quality of the
  linear fit (`payload_report.py`, `series_trend`).

The canary collection keeps, for every payload run, the job's peak
resident memory and request from the PanDA record and, from a finished
run's payload report, the memory summary of each stage. A run removed
for memory by the selection's rule reads "Killed for memory" as its
result. Against the representative's request, the pair settles the
signature as reproduced when the production run is killed again or the
reference run's peak reaches the request; as site dependent when the
production run is killed and the reference peak stays below the
request; as not reproduced when neither happens; and as inconclusive
when a run failed for another stated reason. The measurement is
recorded on the signature (`data['memory_measured']`: run, queue,
stage, peaks, growth, fit, events) and shown on its page. The
settlement's epicprod-live record is titled "Memory kill <key>
<outcome>" and states the outcome in these terms with the measured peak
against the request, never as a crash (2026-11-12; task 37972's
reproduced settlement first read "Both runs crashed" while both runs
completed). Steady growth
with a close linear fit over the whole stage is the shape of memory
accumulated event by event; a plateau reached early is the working size
of the configuration. The page states the numbers; the reading belongs
to a finding.

**Pages.** The catalog and the reproduction runs page carry a Kind
facet ahead of their other facets: Segfault, a payload that died on a
signal it raised itself (SIGSEGV, SIGABRT, SIGBUS, SIGFPE), or Himem, a
job killed from outside for its memory (`?kind=himem`). The runs page's
Result facet holds "Killed for memory".

**Local reproduction and package.** The fragment and the package are
built from a representative as for a crashed job and state the
removal and the request. A run by hand leaves `payload-report.json` and
each stage's prmon series in its working directory, which is the
measurement.

## Bad input

A job whose simulation or reconstruction program stops on an error it
reports does not crash and is not killed: the program exits 1, the
payload exits 1, and the pilot labels the job with a stale container
line (pilot error 1305, "unknown groupid" at GREX, "bind mounting /srv"
at OSG). When the program's FATAL line names the event record, the
fault is the input's, and it recurs on every attempt of the row, so the
row's file is lost after retries: DDSim's "Stable particle (PDG: 2212)
with daughters! check your MC record" and "Cannot Simmulate this MC
Record" lost about a fifth of the files of the 26.07.3 eAu BeAGLE tasks
40856 and 40857 (production finding, 2026-10-22). These failures are
catalogued beside crashes and memory kills as a class of their own
(Torre Wenaus, 2026-10-22).

**Selection.** A failed job of a production task is a bad-input
failure when its payload exited 1 (`transexitcode` 1) and its digest
names the simulation or reconstruction stage as the stage that failed
(`payloadStage`, `payloadStageStatus=fail` in the job metrics). The
record does not carry the program's message, so the selection holds
every reported stop of those stages; the reading below separates an
input fault from a configuration error, which fails every row of its
task at once.

**Signature.** One per task and stage, key `input:task40856:simu`,
class `input`, exit code 1 and no signal, split by stage under the rule
of the memory signatures. The rest of the row is the record across the
task's whole life from aggregate queries: failures, the rate over
finished plus failed, time to failure, and per site the failures and
hosts.

**Representatives.** Three jobs per signature as `EpicProdJob` rows
with `phase = 'payload_input'`, chosen as the memory representatives
are: the shortest, median and longest time to failure among the
failures whose manifest row resolves, kept once chosen.

**The reading.** The program's FATAL line, read from a representative's
payload log, is the signature's reading, recorded on the signature
(`data['input']['fatal']`, with the job it was read from) and shown on
its page. Signatures whose representatives stop on the same line are
one cause.

**The nightly pass.** The inventory's input pass
(`scripts/segfault-inventory.py`, after the memory pass) finds the
tasks with such a failure in its window, recomputes their signatures
over their life, and picks representatives where a signature has none.

**Pages.** The catalog's and the reproduction runs page's Kind facet
gains Bad input (`?kind=badinput`) beside Segfault and Himem.

**Reproduction.** Not yet built: the representative's row run on the
reference queue reproduces the signature when it stops on the same
FATAL line, as an input fault is deterministic.

## Hilog

A job can be chatty about its conditions without failing: a program
that warns on every event, often EICrecon, fills its stage log and the
job may still deliver its output. Such jobs are catalogued as a kind of
their own, Hilog, whether they finished or failed (Wouter Deconinck,
2026-10-27, asking for "a high logging diagnostic to the production
monitoring"; Torre Wenaus, 2026-11-16).

**The measure.** The pilot's log tarball cannot show it: the pilot
keeps the last 1000 lines of each stage, so every tarball is about the
same size however much the job wrote. The payload measures the full
stage logs instead (`<task>.<program>.log`, which `run.sh` tees in full
before passing the last 1000 lines on): its report carries `logs`,
bytes per program and in all, and its digest carries the total in MB,
rounded up, as `pLogMB` (EPICPROD_PAYLOAD.md, The job digest). Both
come with the short job-metric names (`EPICPROD_SWITCH_SHORT_METRICS`),
whose room in PanDA's 500 characters the figure needs; a job written
before them has no figure and is never selected.

**Selection.** A finished or failed job of a production task whose
`pLogMB` is at least `segfault_hilog_mb` (SysConfig, 100). The default
is provisional, to be set from the first readings.

**Signature.** One per task, key `hilog:task42000`, class `hilog`, exit
code and signal 0: a log total is not by stage, so a task holds one.
The rest of the row is the record across the task's whole life from
aggregate queries: the chatty jobs, how many of them failed, the rate
over the task's finished plus failed jobs, their job time, the median
and largest log total, and per site the jobs and hosts. A signature the
threshold no longer yields is retired unless a reproduction is
recorded on it, as a memory signature is.

**Representatives.** Three jobs per signature as `EpicProdJob` rows
with `phase = 'payload_hilog'`, chosen as the memory representatives
are but by log total: the smallest, median and largest among the
chatty jobs whose manifest row resolves, kept once chosen. Each row
carries the job's status and log total.

**The reading.** Which programs wrote the volume, each one's MB
largest first, read once from the first representative whose payload
report carries `logs.by_program` (the sweep's filing, else the PanDA
metatable; `read_hilog_programs` in swf-monitor
`monitor_app/segfaults.py`) and kept under `data['hilog_reading']`,
which a record rebuild leaves alone.

**The nightly pass.** The inventory's Hilog pass
(`scripts/segfault-inventory.py`, after the input pass; `--hilog-only`
for a back-fill, `--no-hilog` to skip) finds the tasks with a chatty
job in its window, adds the tasks already holding a Hilog signature,
recomputes their signatures over their life under the current
threshold, picks representatives where a signature has none and reads
the programs where a signature has no reading. The automatic dig and
the automatic study pass these signatures by: there is no crash to
trace.

**Pages.** The catalog's and the reproduction runs page's Kind facet
gains Hilog (`?kind=hilog`); the signature page states the jobs, the
threshold, the log totals and the programs that wrote them.

**Reproduction.** Not built: a chatty row is read from its report, not
run again.

## Event Service units

An Event Service job (NODE_EVENT_DISPATCHER.md) runs its events as
many units under one PanDA job, and a unit whose simulation crashes
fails alone: its ranges return to the file and the job ends finished.
The crash-class selection, failed jobs exiting 134 to 139, never sees
it. Event Service crashes enter the catalog from the units' own
records (Torre Wenaus, 2026-10-29: "there is a bug, it should be
found").

**The record.** The harness ships each job's record off the node
(JOB_REPORTING.md, The Event Service record), and the sweep files it
on the job's `EpicProdJob` row under `data['es_record']` for every
Event Service job that ended, whatever its status. A failed unit there
carries its unit id, slot, range (one-based `startEvent` and
`lastEvent`), wall time, path (`run.sh` for a slot's first unit,
`resident` for its later ones) and message, and, under `simulation`,
the request the slot sent its resident npsim: the input's LFN and
path, the zero-based first event, the event count and the request
seed. The resident seeds each request's whole random stream with that
seed and uses no per-event seed (NODE_EVENT_DISPATCHER.md, Resident
simulation with external EVGEN input), so the input, the interval and
the seed determine the unit's simulation.

**Selection.** A failed unit is a crash when:

- its message says the resident simulation exited without a reply:
  the resident npsim died during the unit's request;
- its payload exited in the crash class (`payload exit 134` to `139`),
  the run.sh path's cold stages;
- its slot exited on a crash signal (`slot exited -11`, `-6`, `-7`,
  `-8`);
- it is a run.sh unit whose payload stopped in the resident request on
  a request (input, first event, count, seed) already recorded as a
  crash of the task: the same request crashing again on a later pass.

The units failed by a death's consequences (the next request refused
at the dead resident's socket, a range file missing after a restart,
the later units of a stopped resident) and request timeouts are not
crashes. In the records filed to 2026-10-29 the rule finds 12 resident
deaths in 11 jobs of tasks 41494 (7), 41508 (3) and 41497 (2), and
41497's two later passes on the same request.

**Signature.** One per task and stage, key `esunit:task41497:simu`,
class `es_unit`, kind Segfault. Exit code and signal are 139 and 11
once a record or a reproduction names SIGSEGV (each signal as for a
job), and 0 while none does. The signature holds its crash requests
(`data['units']`): each distinct request with the jobs, slots and
passes it crashed in and the seconds from the request's start to the
death. `crashes` counts unit crashes; `rate` is the crashed units over
the units the task's filed records ran; the minutes are the request
times; the signature's jobs are the Event Service jobs carrying the
crashes. A crashed unit's events go back to the file, so the loss is
the events no pass delivered, read from the task's delivery once its
passes are spent, and is not computed here.

**The nightly pass.** The inventory's Event Service pass
(`scripts/segfault-inventory.py`, after the Hilog pass) reads from
swfdb the filed records of the Event Service jobs updated in its
window and rebuilds, over all of their filed records, the signatures
of the tasks with a crash. It reads nothing remote. The automatic dig
passes these signatures by, since the node's logs did not leave it.

**Reproduction.** A crash request is replayed on npps0, through the
npps0 route (NPPS0_WORKER.md), never on the monitor host:
`scripts/segfault-es-reproduce.py --key <key>` (the most repeated
request unless `--unit` names one) runs, in the campaign image of the
job, the payload of the job's version over the unit, as the slot ran
its first unit: run.sh with the unit's skip, chunk and label and the
resident simulation switched on, so the request is the job's own, and
`EPICPROD_UNIT_TEMPLATE` set, so run.sh writes the request's npsim
arguments beside it. When the resident dies, the same arguments run
once more under gdb as a cold npsim over the request's events cut from
the source (`es/evgen_ranges.py`), with the request seed and no
per-event seed, and the backtrace's first frames are the signature's
trace, recorded and merged by frame as a dig's are. The outcome
(crashed, with the signal; clean; inconclusive, with the reason) is a
reproduction entry on the signature with role `reference` and queue
`npps0`, shown on the signature and runs pages. A payload canary of
the manifest row does not replay an Event Service unit: a row runs
under the per-event seed. Task 41497's request (events 631680 to
631689, seed 6317) crashed in npsim and in the resident path and ran
clean under seed 6318 and under the per-event seed.

As built (2026-10-29): the doer is swf-monitor
`scripts/segfault-es-reproduce.py`, run on the monitor host; the driver
it ships is `scripts/segfault-es-replay.sh`, run in the image on npps0.
The doer takes the request's run.sh arguments from the task's manifest
(the file whose name is the request's LFN), the chunk from the request
seed (run.sh seeds chunk *c* with *c* + 1) and the label from the
task's unit size, and the environment from `repro_environment` with the
two resident switches of `production_es.apply`. It ships the payload of
the job's version (the commit that set that version in
`payload/VERSION`), starts the driver detached under
`~wenaus/work/segfault-es/` and polls for its `out/result.json`. The
driver checks that the npsim arguments run.sh wrote carry the recorded
seed before the gdb run. The attempt is an entry on the signature with
`mode` `host`, queue and host `npps0`, role `reference`, recorded as
running when the doer starts and given its outcome, signal and times
when the result arrives; the runs page and the signature page show it
as an attempt with no PanDA job. The production side of the pair is
the record's own passes, which all crashed on the request, so a crash
on npps0 settles the signature as reproduced and a clean run as site
dependent. The trace is read from gdb's backtrace (`trace_extract`,
form `gdb`: "Program received signal" and the frames of `bt`) and
recorded and merged by frame; the signal goes on the signature as 128
plus the signal. The driver's logs, the request's npsim arguments and
the result are kept under
`$SWF_TMP_DIR/segfault-packages/<key>/es-replay-<request id>/`.

The nightly notice lists an untraced Event Service signature among
those for which reproduction is the only extraction left, naming the
npps0 route; the study, the findings and the notice treat a traced one
as any other.
