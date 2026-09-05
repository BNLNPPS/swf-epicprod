# The Rucio failover stash

## Purpose

Production jobs upload their outputs to JLab storage and register them in
JLab Rucio, the catalog of record for science data
(RUCIO_REGISTRATION_CONTRACT.md). When the JLab catalog or the JLab door
is unavailable, that last step fails after the payload work is done.
RUCIO_RESILIENCE.md separates two failure modes. A registration that
fails after a completed upload becomes a pending registration, completed
later by the registrar (Measure 2). An upload path that fails leaves the
output with nowhere to go. This document is the plan of record for the
second case: the output goes to a stash at BNL, catalogued in the BNL
Rucio instance and marked as staging, and is moved to JLab and registered
there when JLab is reachable again.

Two recorded events mark the two modes. On 2026-08-31 about 4,400
finished jobs failed at registration under a coherent load wave while
the transfer path held; Measure 2 covers that. On 2026-09-04 the JLab
authentication endpoint stopped answering for several minutes and every
nightly catalog step that needed it failed; an outage of that shape
during a wave of finishing jobs is what the stash covers.

## What exists

- The BNL Rucio instance, scope `group.EIC`, the PanDA production
  catalog, defines the RSE `BNL_PROD_DISK_1`: a deterministic disk RSE
  behind the dCache door
  `root://dcintdoor.sdcc.bnl.gov:1094/pnfs/sdcc.bnl.gov/eic/epic/disk/`,
  reachable over the wide area. It holds 154.7 TB in 1.15 million files
  as of 2026-09-05, mostly job logs. The RSE reports no free-capacity
  figure, and no quota is recorded for the `eicprod` account. Datasets
  there carry no replication rules; the instance catalogs uploads and
  manages no transfers.
- Every production job already carries a working BNL Rucio credential
  and client. The BNL production queue's environment sets
  `RUCIO_ACCOUNT=panda` and `RUCIO_CONFIG` to the client configuration
  on CVMFS, the pilot's X509 proxy is in the job, and the pilot uploads
  the job's log tarball with them, to `BNL_PROD_DISK_1` or to the
  `JLAB_DISK_1` RSE the BNL catalog also defines, per the queue's
  storage assignment.
- The payload's own uploads go to JLab with a separate configuration:
  the payload's `rucio.cfg`, host `rucio-server.jlab.org`, account
  `eicprod`, the sandbox proxy; the RSE `OUT_RSE`, default `EIC-XRD`;
  the Rucio upload client, which uploads and registers in one call and
  exits 78 on failure (EPICPROD_PAYLOAD.md § The path today).
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
  fails before the transfer completes: the payload uploads the output to
  the stash, records the stash entry in `jobReport.json`, and exits
  success on good physics plus a completed stash upload.

A job spends no more than its bounded attempt on JLab before falling
back. A SysConfig switch, `stash_force`, makes every job stash without
attempting JLab during a declared outage, so a known outage costs no
per-job timeouts.

### The stash

- Location: a BNL dCache space designated by BNL storage operations and
  registered as an RSE in the BNL Rucio instance, written with the
  credential and client the job already carries. `BNL_PROD_DISK_1` is
  the candidate that needs no new RSE or credential, since jobs already
  write their logs to it.
- Naming: the file DID keeps the name the JLab catalog will receive, the
  logical file name under `/RECO/...` or `/FULL/...`, so the move is
  name-preserving and a stash entry names its destination without a
  lookup. Files attach to a stash dataset per task,
  `group.EIC.<taskname>_stash.<jeditaskid>.<jeditaskid>`, parallel to
  the `_log` datasets the pilot writes.
- Metadata on each stash file DID: `staging: true`, the destination
  scope and dataset, campaign, task and job identifiers, the stash time,
  the JLab failure text, the `events` count the registration contract
  requires, and the dataset-level metadata the JLab registration would
  have carried, so the registrar registers at JLab from the catalog
  entry alone.
- No lifetime on stash entries. Deletion is the registrar's act after
  the JLab registration is verified; an entry older than the threshold
  raises an alarm rather than expiring.

### The job report

`jobReport.json` (EPICPROD_PAYLOAD.md § Evolution, payload reporting)
gains a `stash` section: the stash dataset, the files with name, bytes,
checksum, destination dataset and RSE, the reason, and timestamps; and
the `pending_registration` section of Measure 2. Both ride the
success-only metatable channel into the PanDA job record. The registrar
therefore has two sources: the reports, and the BNL catalog's listing of
staging DIDs, which is authoritative when a report is lost.

### The registrar

A production operations agent doer, `stash_drain`, enqueued hourly by
cron and on demand from the pending view, on the prod-ops pattern
(EPICPROD_OPS_AGENT.md). One pass:

1. Lists the staging entries in the BNL catalog by metadata and
   reconciles them with the reports and with its own store, a SQLite
   database beside the storage store,
   `/data/wenauseic/swf-delivery/stash.sqlite`: one row per file with
   its state (stashed, moving, registered, deleted, failed), attempts,
   and timestamps.
2. Probes JLab: authentication and a door check. If either fails, the
   probe is recorded and the pass ends without touching entries.
3. Moves each file to its JLab destination by a third-party copy
   between the BNL door and the JLab door, into the path the JLab
   catalog's deterministic algorithm gives the logical file name, at
   bounded concurrency, a few files at a time, so the JLab door sees a
   trickle rather than a wave.
4. Registers the moved file in JLab Rucio: the replica by logical file
   name at the destination RSE, the attachment to its dataset, the file
   metadata including `events`; then verifies the replica reads
   AVAILABLE.
5. Deletes the stash replica and DID after verification. Every move,
   registration and deletion is an action-stream record with outcome
   and duration (ACTION_STREAM.md). A file whose move or registration
   fails keeps its stash entry and is retried on later passes with
   hour-scale backoff; after `stash_max_attempts` it is marked failed
   for a person.

Retries live only in the registrar (RUCIO_RESILIENCE.md, Measure 2).
The third-party copy is the open technical question: xrootd
third-party copy between the two dCache doors with the production
credentials. If the doors do not support it, the fallback is a streamed
copy through the ops-agent host, which is bandwidth-bound and acceptable
only for small backlogs. The throughput target is a day's backlog
drained in a day: 5 TB per day is about 60 MB/s sustained.

### The pending view

A page under Data, `/pcs/stash/`: staged files by campaign and dataset
with bytes, age, state, attempts and last error; the JLab probe state;
the drain button on the prod-ops pattern, gated to operators, with the
result pushed to the page; a link per row to the JLab dataset page.
Served from the registrar's store; no remote call in render.

### Accounting

- The storage record (STORAGE.md) gains a `stash` block per campaign:
  files and bytes staged, oldest age, and cumulative counters of files
  moved and deleted, read from the registrar's store at projection
  time.
- Alarms (alarms.md): stash age over `stash_stale_hours` at warning;
  stash growth while the JLab probe fails beyond `stash_outage_hours`
  at warning; failed entries at alarm.
- SysConfig keys, present at their defaults: `stash_force`,
  `stash_stale_hours`, `stash_outage_hours`, `stash_max_attempts`,
  `stash_drain_concurrency`.

## Capacity

At the current 5 TB per day average, a one-day outage stashes 5 TB, a
wave of thousands of jobs adds a few TB within hours, and continuous
production for 26.09 raises the rate. The stash needs temporary headroom
of the order of 30 TB, several days of production, retained for days and
released as the registrar drains it, in whatever space BNL storage
operations designate. The RSE's total capacity should be reported so the
fill fraction can be watched.

## Sequencing

1. The allocation and third-party-copy ask to BNL storage operations;
   a third-party-copy test between the two doors with a test file.
2. The registrar's store, the JLab probe, and the drain of a hand-placed
   stash file end to end: move, JLab registration by logical file name,
   verification, deletion, action-stream records.
3. The payload stage, after EPICPROD_PAYLOAD.md stage 0 and the
   registration resilience step: the bounded JLab attempt, the stash
   upload with metadata, the `stash` report section. Acceptance: a
   canary payload run with the JLab RSE made unreachable to the job
   stashes its outputs and the registrar drains them to JLab.
4. The pending view, the drain button, the hourly enqueue.
5. The storage record's stash block and the alarms.

## Asks and open items

- BNL storage operations: the space for the stash, temporary
  science-data overflow of the order of 30 TB, with `BNL_PROD_DISK_1`
  as the candidate; third-party copy from its dCache door to the JLab
  door with the production credentials; the RSE's total-capacity
  figure.
- JLab storage and Rucio operations: third-party copy into the JLab
  door; registration of an existing replica by logical file name for
  the `eicprod` account; confirmation that the copy lands on the path
  the catalog's deterministic algorithm expects.
- BNL Rucio administration: the `panda` account writing science-sized
  stash datasets in `group.EIC`; the `_stash` dataset convention;
  path-like DID names under the BNL instance's naming policy.
- The production team: agreement that a job with a stashed output exits
  success, the contract change of Measure 2.

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
