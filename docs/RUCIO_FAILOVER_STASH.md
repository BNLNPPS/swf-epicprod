# The Rucio failover stash

## Purpose

Production jobs upload their outputs to storage and register them in
JLab Rucio, the catalog of record for science data
(RUCIO_REGISTRATION_CONTRACT.md). When the catalog or the destination
door is unavailable, that last step fails after the payload work is
done. RUCIO_RESILIENCE.md separates two failure modes. A registration
that fails after a completed upload becomes a pending registration,
completed later by the registrar (Measure 2). An upload path that fails
leaves the output with nowhere to go. This document is the plan of
record for the second case: the output is written to the BNL
science-data RSE at the path its logical name resolves to, so that it is
already home, and the catalog of record is given its replica row when
JLab is reachable again.

Two recorded events mark the two modes. On 2026-08-31 about 4,400
finished jobs failed at registration under a coherent load wave while
the transfer path held; Measure 2 covers that. On 2026-09-04 the JLab
authentication endpoint stopped answering for several minutes and every
nightly catalog step that needed it failed; an outage of that shape
during a wave of finishing jobs is what the stash covers.

## What is built

- **The payload stashes** (epicprod-payload 0.11.0): when a JLab
  registration fails outright, the output is written with `xrdcp` to
  the stash RSE's write door at the deterministic path of the file's
  logical name, with the production credential the job already carries,
  and recorded in the payload report: the logical name, the path, the
  registration it is owed, and why. The job exits on its physics. A
  stash that refuses the file too is still exit 78.
- **The drain** (`swf-monitor/scripts/stash-drain.py`, ops-agent handler
  `stash_drain`, hourly at :47): reads what jobs stashed from their
  reports, confirms each file at the door with its size and checksum,
  probes JLab, and when the catalog answers registers each file by
  logical name at the stash RSE with its event count, through the
  registrar's own registration, and verifies the replica reads
  AVAILABLE. Nothing is copied and nothing is removed. A file that will
  not register keeps its entry and is tried again hourly, eight times at
  most, with its reason kept in the drain's state file beside the
  storage store. An output under `/TEST/` carries a seven-day lifetime
  once registered, as the payload canaries' do. `--entry <logical name>`
  registers a file already at its deterministic path with no report, the
  hand test of the path and the operator's tool for a lost report.
- **The pending view** (`/pcs/stash/`, in the Data menu and on the nav
  dashboard): what is waiting and what it owes, how far each entry got,
  attempts and last error, what the last pass registered, and the JLab
  probe state, from the drain's own account.
- **Not built yet**: the storage record's stash block, the alarms, the
  SysConfig keys they read, and the listing of the door under the
  datasets a task owes, the authority for a stashed file whose report was
  lost.

The stash was first built, on 2026-09-07, on a BNL dCache space
catalogued in the BNL Rucio instance, with a drain that streamed each
file to its destination RSE and deleted the stash copy. Since every
production configuration already sends its output to BNL-XRD, that
design carried a file from one BNL door to another to reach the RSE it
was meant for. It was replaced on 2026-09-08 by the design above, which
data management proposed: place the file where a replica of it belongs,
and let the catalog catch up. The BNL-instance stash, its flat naming
and JSON-plugin metadata, the streamed copy through the ops-agent host,
and the deletion the `panda` account was not permitted to make are all
retired with it.

Implementation facts, each measured rather than assumed. Rucio's upload
client needs gfal2, which the ops-agent host does not have, so the
catalog work is done with `add_replicas` against a file the storage
already holds. xrootd refuses a credential whose file permissions are
wider than 0600, so the drain keeps a private-mode copy of the proxy and
refreshes it from the source, as the EVGEN doer does. An RSE's read door
can be a port with no write at all (BNL-XRD reads on 1095 and writes on
1094), so the payload writes to the write door and the registrar reads
back through the RSE's own protocol entry.

## What exists

- `BNL-XRD` in the JLab catalog: a deterministic disk RSE at BNL, write
  door `root://epicxrd1.sdcc.bnl.gov:1094`, prefix `/eic/EPIC`, read
  door on port 1095. It held 755 TB in 2.1 million files on 2026-09-08
  and reports no free-capacity figure. Every current production
  configuration names it as `OUT_RSE`, so for production as run today
  the stash and the destination are the same RSE, and a catalog outage
  during upload is a plain pending registration.
- The payload's uploads go through its `rucio.cfg`, host
  `rucio-server.jlab.org`, account `eicprod`, the sandbox proxy; the RSE
  `OUT_RSE`; the Rucio upload client, which uploads and registers in one
  call and exits 78 on failure (EPICPROD_PAYLOAD.md § The path today).
  The same proxy is accepted by the BNL-XRD write door, measured
  2026-09-07.
- Volume: campaign 26.07 registered 496,047 RECO files, 265 TB, and
  14,560 FULL files, 8.8 TB, between 2026-07-13 and 2026-09-05, about
  5 TB per day on average, with waves of several thousand jobs finishing
  within hours. The mean RECO file is 535 MB.

## Design

### Trigger

The payload's output step makes one bounded attempt at JLab:
authentication and upload timeouts of the order of a minute each, no
retry in the job. Three outcomes:

- upload and registration succeed: as today;
- upload succeeds and registration fails: the job records a pending
  registration in `jobReport.json` and exits success on good physics
  plus a completed upload (Measure 2);
- the upload itself fails, because the door is unreachable, the JLab
  catalog refuses or times out on authentication, or the upload client
  fails before the transfer completes: the payload writes the output to
  the stash RSE at its deterministic path, records the stash entry in
  `jobReport.json`, and exits success on good physics plus a completed
  stash upload.

A job spends no more than its bounded attempt on JLab before falling
back. A SysConfig switch, `stash_force`, is to make every job stash
without attempting JLab during a declared outage, so a known outage
costs no per-job timeouts; it is not present yet.

### The stash

- Location: `BNL-XRD`, the BNL science-data RSE of the catalog of
  record, written through its write door with the production credential
  the job carries for its JLab uploads. No second catalog, RSE or
  credential is involved.
- Naming: the file is written at the path the RSE's deterministic
  naming gives its logical name, `/eic/EPIC` followed by the name, so
  the stash entry and the replica the catalog is owed are one and the
  same file. The report carries the logical name, the path, and why the
  file went there.
- A file present at its deterministic path without a catalog row is a
  pending registration, never garbage: a later attempt at the same name
  adopts it when size and checksum match rather than replacing it. The
  registrar registers it; the payload's pre-upload check treats it as
  delivered once registered.
- No lifetime on stash entries. An entry older than the threshold
  raises an alarm rather than expiring.
- Other non-JLab RSEs of the catalog could take the stash by the same
  rule when BNL-XRD is full or unreachable, with a door and credential
  check per RSE. Only BNL-XRD is used.

### The job report

`jobReport.json` (EPICPROD_PAYLOAD.md § Evolution, payload reporting)
carries a `stash` section: the files with logical name, path, the
registration owed, and the reason; and the `pending_registration`
section of Measure 2. Both ride the success-only metatable channel into
the PanDA job record. For a report that is lost, the authority is the
door itself, the files at deterministic paths under the datasets a task
owes that the catalog does not know; the listing is not built yet.

### The registrar

A production operations agent doer, `stash_drain`, enqueued hourly by
cron and on demand from the pending view, on the prod-ops pattern
(EPICPROD_OPS_AGENT.md). One pass:

1. Reads the stash entries from the reports and reconciles them with its
   own state, a JSON file beside the storage store,
   `/data/wenauseic/swf-delivery/stash-drain-state.json`: one entry per
   file with its outcome (home, failed), attempts, last attempt and
   reason.
2. Confirms each file at the stash door with the size and checksum the
   storage reports.
3. Probes JLab. If it does not answer, the probe is recorded and the
   pass ends without touching entries.
4. Registers each file in JLab Rucio: the replica by logical name at the
   stash RSE, the attachment to its dataset, the file metadata including
   `events`; then verifies the replica reads AVAILABLE. Every
   registration is an action-stream record with outcome and duration
   (ACTION_STREAM.md). A file whose registration fails keeps its entry
   and is retried on later passes with hour-scale backoff; after
   `stash_max_attempts` it is marked failed for a person.

Retries live only in the registrar (RUCIO_RESILIENCE.md, Measure 2). A
later move of a registered file to another RSE is Rucio's, by rule and
FTS, and no concern of the stash.

### The pending view

A page under Data, `/pcs/stash/`: staged files by campaign and dataset
with bytes, age, state, attempts and last error; the JLab probe state;
the drain button on the prod-ops pattern, gated to operators, with the
result pushed to the page; a link per row to the JLab dataset page.
Served from the registrar's store; no remote call in render.

### Accounting

- The storage record (STORAGE.md) gains a `stash` block per campaign:
  files and bytes staged, oldest age, and cumulative counters of files
  registered, read from the registrar's store at projection time.
- Alarms (alarms.md): stash age over `stash_stale_hours` at warning;
  stash growth while the JLab probe fails beyond `stash_outage_hours`
  at warning; failed entries at alarm.
- SysConfig keys, to arrive with the alarms that read them:
  `stash_force`, `stash_stale_hours`, `stash_outage_hours`,
  `stash_max_attempts`. The drain today takes its attempt ceiling from
  the environment (`STASH_MAX_ATTEMPTS`, default 8).

## Capacity

At the current 5 TB per day average, a one-day outage stashes 5 TB, a
wave of thousands of jobs adds a few TB within hours, and continuous
production for 26.09 raises the rate. Because the stash is the
production RSE itself, an outage adds to BNL-XRD what production would
have written to it anyway; what changes is the timing of the catalog
rows, not the volume. The RSE's total capacity should be reported so the
fill fraction can be watched.

## Sequencing

1. The registrar's store, the JLab probe, and the registration of a
   hand-placed file at its deterministic path end to end: verification,
   JLab registration by logical name, AVAILABLE read back,
   action-stream records.
2. The payload stage: the bounded JLab attempt, the stash upload at the
   deterministic path, the `stash` report section. Acceptance: a canary
   payload run with the JLab catalog made unreachable to the job
   stashes its outputs and the registrar registers them.
3. The pending view, the drain button, the hourly enqueue.
4. The storage record's stash block, the alarms, their SysConfig keys,
   and the door listing for lost reports.

Steps 1 to 3 were completed by 2026-09-08 on the BNL-XRD design, step 1
on 2026-09-08 with a hand-placed file under `/TEST/`. Step 2's
acceptance, a canary payload run with the JLab catalog unreachable to
the job, has not been run. Step 4 is outstanding.

## Asks and open items

Open:

- BNL storage operations: the total capacity of BNL-XRD (epicxrd1), so
  the fill fraction can be watched, and awareness that an outage brings
  production's output there a few days early rather than not at all.
- The production team: agreement that a job with a stashed output exits
  success, the contract change of Measure 2, and that a file already at
  its deterministic path with matching size and checksum is adopted at
  registration rather than replaced.

Answered by measurement, and so never asked:

- Registration of an existing replica by logical file name for the
  `eicprod` account at BNL-XRD, and the path the deterministic algorithm
  expects: both proved on the 2026-09-07 probe and again on 2026-09-08.
- The credential: the BNL-XRD write door accepts the production proxy
  the job already carries.

## Related

- [RUCIO_RESILIENCE.md](RUCIO_RESILIENCE.md): the two measures; the
  stash is the last clause of Measure 2.
- [EPICPROD_PAYLOAD.md](EPICPROD_PAYLOAD.md): the payload in which the
  trigger and the stash upload are implemented.
- [RUCIO_REGISTRATION_CONTRACT.md](RUCIO_REGISTRATION_CONTRACT.md): the
  metadata every registration carries, stash included.
- [STORAGE.md](STORAGE.md): the storage record the stash block joins.
- [EPICPROD_OPS_AGENT.md](EPICPROD_OPS_AGENT.md): the doer pattern the
  registrar follows.
