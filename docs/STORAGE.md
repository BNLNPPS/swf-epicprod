# Storage — the placement record and the Storage view

Production data is placed on Rucio Storage Elements (RSEs): a job uploads
its output to one RSE and registers it in JLab Rucio, replication rules
copy it to further RSEs and to tape, and the deletion daemon removes what
rules no longer hold. The catalog records what is placed where at the
present moment and keeps no history of replica states, so a transfer
backlog, a stuck rule, a file registered but never uploaded, or a
campaign left on a single copy is visible only while it lasts and only
to someone who looks. This document defines the storage record: a
Snapper component that samples the placement state of production data
on every RSE, the pass that maintains it, the view that shows the data
lifecycle per RSE, and the retrieval surface that serves the listings
behind every count. It follows the delivered-data record
([CAMPAIGN_DELIVERY.md](CAMPAIGN_DELIVERY.md)), which records what a
campaign delivered; this record covers where the data is, how it got
there, and what is wrong with it.

The Snapper concepts, the SWF deployment, and the display laws are in
the snapper-ai documentation
([DESIGN.md](https://github.com/BNLNPPS/snapper-ai/blob/main/docs/DESIGN.md),
[INTEGRATION.md](https://github.com/BNLNPPS/snapper-ai/blob/main/docs/INTEGRATION.md),
[TIME_HISTORY_UI.md](https://github.com/BNLNPPS/snapper-ai/blob/main/docs/TIME_HISTORY_UI.md))
and in swf-monitor
([SNAPPER.md](https://github.com/BNLNPPS/swf-monitor/blob/main/docs/SNAPPER.md)).
The component idioms used here are established by the PanDA activity,
error-state and platform components: cumulative counters differenced
by every consumer, gauges at the instant, interval-stamped
publications, and bounded exception listings with exact overflow
folding.

## Historical questions

Registration begins with the questions the record must answer
(DESIGN.md, invariant 6):

- At an instant, how much production data was on each RSE, in what
  replica state, for which campaign and data root.
- Between two instants, what arrived at each RSE, as first copies from
  jobs and as replicas by rule; what transfers completed; what was
  deleted.
- What was waiting: transfers in flight and their age, rules stuck,
  datasets partially placed, datasets whose arrivals stopped while
  their task still ran.
- How fast the pipeline ran: job end to registration, registration to
  availability, first copy to second copy, disk to tape.
- How well each campaign was protected: single-copy files, disk-only
  and tape-only data, the archival backlog.
- What was wrong in the catalog: files registered with no available
  replica anywhere (ghosts), files attached to no dataset, files
  without the event count the registration contract requires; and
  whether those populations shrink between campaigns or grow.

The useful resolution is hourly for the live record, with a full
reconciliation nightly.

## The pass

The pass is the owner's maintenance of the projection (DESIGN.md,
invariants 1 and 4): it reads the catalog, keeps its own per-file and
per-dataset state, and publishes the bounded component. It is a crawl:
one dataset location at a time, two in flight, a pause after every
catalog call, and never more than one location's file names in hand,
so its memory stays at the size of one dataset whatever the size of
the inventory (about 8.8 million files under the roots). Every row a
pass touches is stamped with the pass, which makes the gone check a
query per location and an interrupted pass resumable from where it
stopped. A pass holds the store only while the process that opened it
is alive, so a pass killed with its process never blocks the next one,
which notes the abandonment and redoes the interval; a signalled pass
records the interruption on its row. The counters a location's
transitions accrue are written in the transaction that commits its
rows, so an interruption loses nothing observed. It reads at three
tiers, each from a call the
residual-rerun and delivery paths already use against JLab Rucio.

**Dataset tier**, every dataset under the production roots (about 6,400
today under `/RECO`, `/FULL` and `/EVGEN`): the per-RSE dataset replica
summary, which carries registered and available file counts, bytes,
state, and the replica's creation and update times per RSE; and the
dataset's replication rules, which carry the rule state, RSE
expression, lock counts by OK, replicating and stuck, and the stuck and
expiry times. Two calls per dataset. This tier alone yields transfers
in flight, stuck rules, partial datasets and per-RSE first arrivals.

**File tier**, the target campaigns (the current, last and producing
campaigns the delivery record covers) and every dataset with no RSE
holding all of its files available: the file DIDs from the name search per campaign,
which lists files whether or not they are attached to a dataset, and
their replica states by RSE from the replica listing in all states, by
DID in batches of a thousand at about a second per batch. The bulk
metadata call the delivery rebuild already makes supplies DID creation
time, bytes and the events attribute.

**RSE tier**: usage per RSE (used, total, file count) and the production
account's usage against its limits.

The pass keeps its own store, a SQLite database beside the file-events
store: one row per file with its campaign, root, dataset path, bytes,
creation time, replica states by RSE, the time each replica was first
observed available, attachment, and event count; one row per dataset
with its per-RSE summary and rules; one row per pass with its mode,
coverage and duration. Transitions are derived by comparing a file's
states with its previous row, so appearance, completion, deletion and
clearance are observed at pass cadence; a file's replica carries no
timestamp of its own, and a copying replica's age is measured from the
DID's creation, an upper bound.

Three modes:

- **census**, once, over every file under the roots, establishing the
  complete inventory and the initial ghost population;
- **full**, nightly as a `catalog_sync` chain step, over the dataset
  tier for every dataset and the file tier for the target campaigns;
- **incremental**, hourly, over files registered since the previous
  pass (the arrivals sweep's created-after search) and every file whose
  previous row held a non-available replica, plus the dataset tier for
  datasets touched by either.

The job join: every production job records its manifest row as a
pseudo-input file named by the sequence number, and an output file's
name carries the same row as its chunk index, so an arrived file maps
to its job, the job's end time and its compute site with one query of
the PanDA job records. The pass uses the join for stage-out attribution
and the job-end-to-registration latency.

## The storage component

A component, internal name `storage`, in the epicprod scope, published
by the pass after each run. Publisher identity `swf-monitor:storage`,
assessment policy `swf-storage-v1`, schema version 1, canonical JSON
bounded at 64 KiB, the bound the PanDA activity and platform components
carry. Snaps are full-only, so a component's size costs on every
epicprod snap regardless of its own cadence; the listings that would
make this record large live in the pass's store and are served live.

Each publication covers the interval `(previous source time, now]`.

**Interval and pass** (kind: window; provenance). The interval; the pass
mode, the campaigns covered, files and datasets checked, duration, and
any source that failed to read, recorded in place.

**Per RSE** (bounded map; every JLab RSE retained, 16 at most):

- type, disk or tape;
- capacity (gauge): used and total bytes, file count, fill fraction
  where the storage reports a total, and the usage record's time;
- inventory (gauge): files and bytes by replica state; the same by
  campaign for the target campaigns with the remainder folded into
  `other`; and by root;
- datasets (gauge): total, complete, partial, empty and unavailable on
  this RSE, from the dataset replica summaries;
- rules (gauge): rules by state whose expression names this RSE; locks
  OK, replicating and stuck; the oldest stuck age; rules expiring
  within thirty days;
- backlog (gauge and assessment): copying files and bytes; the age
  distribution as median, 90th percentile and maximum; the count over
  the stuck threshold;
- ghosts (gauge): files and bytes with no available replica on any RSE
  whose non-available replica is here, by state and by campaign, and
  the oldest age;
- stageout (cumulative counters, bounded map of 32 sites): first copies
  landed here per compute site, files and bytes, attributed through
  the job join;
- flow (cumulative counters): arrived files and bytes, split into first
  copies and later replicas; transfers completed; files and bytes
  deleted; ghosts appeared and cleared; bad replicas appeared.

**Per campaign** (bounded map of the target campaigns, 8 at most, the
remainder folded):

- files and bytes registered;
- protection (gauge): single-copy files, two or more copies; disk-only,
  tape-only, disk and tape;
- unattached files, files without the events attribute, archival
  backlog bytes (on disk, not on tape);
- datasets (gauge): total, open, empty, partial on every RSE, stalled
  (open, no arrival within the stalled threshold, producing task not
  final);
- flow (cumulative counters): arrived files and bytes, archived files
  and bytes, jobs finished;
- latency (assessment over the interval's arrivals): job end to
  registration, registration to availability, first to second copy,
  disk to tape, each as count, median and 90th percentile, at pass
  resolution.

**Exceptions** (bounded listings): ghosts as name, RSE, state, campaign,
bytes and creation time; stuck rules as dataset, RSE, stuck time and
stuck lock count; stalled datasets as dataset, campaign, last arrival
and task. Each listing carries at most fifty rows, the head of the
store's list ordered oldest first, with the exact remainder as an
overflow count. The full lists are retrieval, below.

**Thresholds and assessment.** The stuck, stalled and single-copy-age
thresholds are SysConfig keys present at their defaults; the
`assessment` block carries per-RSE and per-campaign verdicts against
them, the thresholds applied, and the overall verdict.

Cumulative counters are monotonic from the census, with an arbitrary
absolute origin: every consumer differences two instants, and the view
renders them window-relative or as per-interval bins. A quiet pass, in
which no counter and no gauge changed, is affirmed unchanged with the
source time advanced, so intervals tile and quiet hours write no snap.

### Backfill

Arrivals reconstruct: Rucio keeps every DID's creation time and every
dataset replica's creation time per RSE. A backfill script writes
`backfill-storage-v1` snaps on a daily grid carrying the arrived
counters per RSE and campaign with the census's absolute origin, on
the PanDA counter precedent, attributing a file's first copy to its
sole RSE or to the RSE whose dataset replica was created first.
Transfers, deletions, ghost appearance and clearance, ages and
latencies do not reconstruct; those families begin at the census, and
the view states the record's start.

## The Storage view

A dedicated focus view, `Storage`, on the mechanism the Site, Errors and
Platform views use: its own clean path under the epicprod scope
(`/snapper/epicprod/storage/`), a focus-sized cached series over the
`storage` component's snaps, and its own detail rendering. It is the
data-lifecycle counterpart of the Site view, which is the job lifecycle
per queue.

**Parameters**, in conformance with the other focus views:

- focus `rse`: one option per JLab RSE, default all; with several shown,
  presentation follows the peak arrival rate over the window, idle RSEs
  last and closed, with the jump list;
- grouping selector: by campaign, by replica state, or by data root;
- counting selector: files or bytes;
- the window, cut, zoom and curve selection the page carries for every
  view; default window 30 days.

**Families per RSE**, panel order following the lifecycle, rates first,
then backlogs and latencies, then state:

1. *Arrivals* — first copies and replicas per interval, from the flow
   counters, binned at render.
2. *Transfers and deletions* — completed transfers and deletions per
   interval.
3. *Backlog* — copying files stacked by the grouping, with the count
   over the stuck threshold; the backlog age as a small panel.
4. *Ghosts* — the ghost population stacked by the grouping; appeared and
   cleared per interval beneath it.
5. *Inventory* — files or bytes by replica state, stacked; the grouping
   selects campaign or root instead of state.
6. *Rules* — locks replicating and stuck.
7. *Capacity* — the fill fraction, where defined.

**Scope-level families**: campaign protection as single-copy, disk-only,
tape-only and disk-and-tape stacks per campaign; the archival backlog;
catalog quality as ghosts, unattached files and files without an event
count per campaign; the arrival yield, files arrived over jobs finished
per campaign, derived at series time; latency medians per campaign as
small panels; and the stage-out matrix, first copies per compute site,
under the RSE it landed on.

**Consequences**: the error-state component's data-management and
stage-out failure events beneath the panels, on the Platform pattern,
attributed to the destination RSE through the task's output setting.

**The cut.** A click is a time cut. The card renders the RSE's pipeline
state at that instant: what landed in the detail window, what is
waiting and for how long, what failed, capacity and rules, and the
exception listings, each row linking to its dataset page. The card
states the interval basis once. Counts beyond the component's listing
heads fetch from the store live, as the errors card fetches its
diagnostic patterns.

## Retrieval

The component rides the Snapper retrieval surface unchanged: the REST
endpoints and MCP tools answer inventory, backlog and ghost counts at
any instant with the standard evidence envelopes, and `changes_between`
locates transitions.

The unbounded listings are served from the pass's store by an MCP tool,
`epicprod_storage`, and its REST counterpart: the ghosts, stuck rules
and stalled datasets, filtered by RSE and campaign, with the file's
state, bytes and creation time. The ghost list for one RSE is what an
operator hands to the Rucio administrators for cleanup.

The same listings have a page, Storage exceptions, under the Data
menu (`/pcs/storage/<listing>/`): the three listings as page tabs with
their totals, RSE, campaign and state filters carried in the URL with
the count each choice would show, the ghost account by holding RSE
above the ghost rows, every dataset linked to its DID page, and the
whole filtered list downloadable as a CSV or as bare names, the form
handed to the administrators. The page states which pass the record
reaches and whether a pass is writing the store. It reads the store
only. The ghost population it serves is a cached product, stamped
with its build time and rebuilt by the sweep after every pass; the
page's Update button rebuilds it on demand.

## Detection and notice

Planned, after the record and the view: the alarm engine reads the
latest published component on each tick and raises one detection per
verdict in warning: ghosts growing over consecutive passes at an RSE,
transfers stuck beyond the threshold, datasets stalled, a campaign
carrying single-copy data beyond the single-copy age, and an RSE
filling beyond its threshold. The thresholds stay with the record; the
alarm carries only its own parameters, as the platform-health alarm
does.

## Implementation notes

- swf-epicprod: `swf_epicprod/analytics/storage.py` (the pass, the
  store, the projection); `swf_epicprod/analytics/storage_listings.py`
  (the exception listings from the store, read-only; the ghost
  population is the cached product `storage_ghosts:v1` on the
  swf-monitor mechanism, served stored, rebuilt by the sweep as its
  last step through `refresh_ghost_product`, with a 90-minute TTL as
  the safety net) behind the MCP tool `epicprod_storage`
  (`swf_epicprod/mcp_tools/storage.py`), the REST listing
  `pcs/api/storage/<listing>/` (`pcs/api_views.py`) and the Storage
  exceptions page (`pcs/views.py`); `scripts/backfill_storage_arrivals.py`.
- swf-monitor: `monitor_app/snapper_storage.py` (registration and
  publication, beside the delivery maintainer); the doer
  `scripts/storage-sweep.py` with `--census`, `--full` and the default
  incremental mode, `--resume` for an interrupted pass and
  `--publish-only` to publish the store's last completed pass without a
  crawl, invoked as the `storage_sweep` chain step after the
  delivery rebuild and by an hourly cron enqueue of the `storage_sweep`
  message; provider additions in `snapper_providers.py` (curve
  extraction under a `st` prefix family, the families, the focus view,
  the card); the card kind in `_snapper_cards.html`; the tool registry
  entries for `epicprod_storage`; the SysConfig keys `storage_copying_stuck_hours`,
  `storage_stalled_hours`, `storage_single_copy_warn_days`.
- Store: `/data/wenauseic/swf-delivery/storage.sqlite`, beside the
  file-events store.
- The series cache version bumps with the new curve vocabulary; the
  focus series cache takes the live 90-second class.
- Order of delivery, each stage usable on its own: the census and the
  store; the component and its publication on the chain and the hourly
  cron; the view with its cut card; the retrieval tool; the arrivals
  backfill; detection.

## Related

- [CAMPAIGN_DELIVERY.md](CAMPAIGN_DELIVERY.md) — the delivered-data
  record; the target campaigns and the file-events store this record
  shares.
- [EPICPROD_DATA_LINEAGE.md](EPICPROD_DATA_LINEAGE.md) — the arrivals
  sweep, the Rucio snapshot, and the task output records.
- [RUCIO_REGISTRATION_CONTRACT.md](RUCIO_REGISTRATION_CONTRACT.md) — the
  event count every registration must carry.
- [RUCIO_RESILIENCE.md](RUCIO_RESILIENCE.md) — registration losses under
  load; the pending-registration backlog this record will measure.
- [JEDI_INTEGRATION.md](JEDI_INTEGRATION.md) — output naming and the
  residual rerun's arrival check.
- swf-monitor [SNAPPER_PLATFORM.md](https://github.com/BNLNPPS/swf-monitor/blob/main/docs/SNAPPER_PLATFORM.md)
  and [SNAPPER_ERRORS.md](https://github.com/BNLNPPS/swf-monitor/blob/main/docs/SNAPPER_ERRORS.md)
  — the component idioms this record follows.
