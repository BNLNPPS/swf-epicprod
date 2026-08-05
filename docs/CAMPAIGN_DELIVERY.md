# Campaign Delivery — the delivered-data record and its views

The crucial campaign deliverable is available data: events delivered
and accessible, stated in physics-configuration terms, absolute and as
a fraction of what the campaign is set up to produce. Today the
epicprod Snapper scope records the production machinery (jobs, tasks)
and nothing records the deliverable. This plan defines the
delivered-data record, its historical backfill, the PCS extensions it
requires, and the surfaces it feeds.

## Metrics

- **Events available** per physics configuration (PC) — the primary
  metric. Events are not reported by the pilot in practice (PanDA
  `nevents` is unpopulated for these payloads; verified 2026-07-27),
  so delivered events = files placed × events/file, with events/file
  equal to the events/job fixed in the production configuration.
- **Completion fraction** = events available / expected events. The
  denominator follows a recorded precedence chain per PC:
  **campaign-included** (what the campaign is set up to produce) →
  **requested** (the original request) → **absent**. Where absent, no
  fraction is shown; the PC carries an unobtrusive, visible no-target
  marker, the same marker in every surface. A future automated
  campaign processing system requires the campaign-included number,
  which is therefore recorded as production data.
- **Bytes placed** — the practical companion metric. The disk/tape
  split and the disk fraction of total capacity are wanted but second
  order (operations rather than delivery); live capture only, since
  Rucio retains no lock-state history to reconstruct the split.
- **Arrivals/day** — the delivery rate, at daily granularity.
- Excluded from this record: stuck files and transfer state, which are
  production operations and surface there, and assessment verdicts;
  the views present information for the reader's own assessment.

### Composition

A group's absolute is the sum of its PCs' delivered events. A group's
fraction is Σdelivered / Σexpected over its constituent PCs, weighting
each PC by its expected events. With mixed target coverage the
fraction is computed over the PCs that have targets and labeled with
its coverage ("82% of target, over 14 of 17 PCs with targets").

## The record

A **campaign delivery** Snapper component in the epicprod scope, one
per active campaign, published by the catalog-sync/analytics chain
(the same owner-publishes pattern as the panda component) and curated
to sweep cadence, so every recorded change is a change worth keeping.

The component JSON is **leaf-keyed by PC**: per PC — events available,
expected events with its provenance tier, files, bytes. Two
consequences:

- **Lenses are read-time projections.** A categorization — physics
  category, PWG, DSC, and later additions — is an N-way labeling of
  PCs held in PCS as named tag-sets; the labelings may overlap. The
  series builder sums leaves into the chosen lens's group curves at
  extraction time, so a lens defined later re-plots the entire record,
  backfill included, without re-capture.
- **Drilldown serves from the same snap.** Curves draw at lens-group
  level; the cut card lists a group's samples with their completion at
  that instant, each linking to its PCS dataset page. Deep per-RSE and
  rule detail stays live in PCS and Rucio pages, outside the record.

## The daily record

The record is reconstructible at any time: JLab Rucio file DIDs carry
registration times and sizes, and events/file is time-independent
configuration. Events and bytes arrival series therefore reconstruct
exactly, keyed to PCs through the DID–PCS mapping, at daily
resolution on Eastern-Time calendar days. Reconstruction is the
production method: each build rebuilds the record in full, so
historical and current days are one record with one producer. Snaps
carry capture policy `delivery-daily-v1`, one per complete ET day,
stamped at day end. The placement split does not reconstruct (no
lock-state history) and is deferred with the disk/tape metric.

### Nightly production

A `delivery_daily_rebuild` step of the nightly `catalog_sync` chain
(the production-operations agent, 02:47 ET) performs the rebuild: the
full file inventory is read from Rucio with per-file registration
times and sizes, locations are mapped to physics configurations
through the PCS task output records, events are joined from the
measurement store, and every complete ET day through yesterday is
written, replacing the previous build. Full reconstruction on every
run means newly measured events, newly mapped locations, and revised
denominators refine the entire history, and a missed night is covered
by the next run with no cursor state. The rebuild covers every
campaign already in the record, the current and last lifecycle slots,
and any campaign currently producing — recorded history is never
dropped, and the metadata pass scales with active campaigns, not the
full catalog. Unmapped locations and files not resolving to a target
campaign are counted in the build summary, never dropped silently.

The preceding `file_events_measure` step keeps the measurement store
current (The events source, below). Both steps record their outcome
in the epicprod action stream, and the `delivery_record_freshness`
alarm fires when the newest daily snap is older than 30 hours — one
missed night. At step completion the agent buffers one Capcom notice
(source `swf-campaign-delivery`) in the monitor's notice store
(`/api/capcom/notices/`): the newest recorded day's
arrivals linking to the campaign view, or a warning on failure. Feed
consumers poll the store from their own side; no external feed
credential is held in SWF. Build logic: `swf_epicprod/analytics/delivery_daily.py`,
invoked by the agent through
`swf-monitor/scripts/delivery-daily-rebuild.py`; hand runs use
`scripts/backfill_delivery_history.py` (dry-run default).

### The events source

No per-output-file event count is recorded anywhere the system reads
(verified 2026-07-28, confirmed with the production operator): the
live production tasks bind no configuration carrying a rate, Rucio's
native `events` field is unpopulated at registration, and the condor
chunker computed each submission's chunk size from that day's timing
without retaining the chunk lists — chunk sizes therefore vary by
dataset and submission. Two recorded facts substitute: Rucio has every
file's size, and the ANL campaign catalog (the timing feed the condor
submitter reads) has exact event totals per EVGEN source file.

`swf_epicprod/analytics/file_events.py` derives per-file events from
them — nightly as the `file_events_measure` chain step, by hand via
`scripts/measure_file_events.py`:
files in one dataset location cluster into uniform byte-size classes,
one per chunking; one xrootd read per class (the `events` tree entry
count, via uproot on a disk replica) anchors the class rate, and
members inherit it. Classes with no readable replica (tape-only) are
derived from the catalog where the source's chunks are fully delivered
and the location is dormant; derived rows recompute on every run.
Provenance per file: `measured`, `sampled-rate`, or `catalog-derived`.
Results accumulate in a SQLite store
(`/data/wenauseic/swf-delivery/file_events.sqlite`) that the daily
record builder joins at build time, emitting per-PC `arrived_events`,
cumulative `events`, and an explicit `unmeasured_files` count — event
sums are floors wherever measurement is incomplete, and the campaign
view states that coverage. Catalog totals cross-check the assignment
per location.

## PCS extensions

1. **Campaign-included expected events per PC** — a validated field on
   the PC's campaign record, set at campaign assembly from the request
   and configuration, adjusted when dispositions change scope. The
   requested count is retained separately; each denominator records
   its provenance tier (included / requested / derived). For 26.06 and
   26.07 this is a one-time curation: prefilled from bound
   configurations and EVGEN input dataset sizes, hand-declared where
   configurations are placeholders, provenance-marked either way.
2. **Events/job in adopted configurations** — the placeholder-config
   backfill (e.g. the association-sweep adoptions), which the events
   metric depends on.
3. **N-way categorization assignment** — the lens labelings a PC can
   carry N of, with the assignment surface. These live on the
   `PhysicsConfig` entity (PCS.md § Datasets), whose first association
   is the requesting-group list seeded from PC-anchored requests.

## Surfaces

1. **Campaign plan list** (PCS): the Physics Configurations page's row
   spine, scoped to one active or future campaign's included and
   proposed PCs, with **target events as a number column** — filled
   where set, visibly missing where not. This page is the curation
   surface for the denominator (per-row entry, bulk prefill for the
   backfill) and makes campaign membership explicit before production
   exists. The list of PCs with targets constitutes the campaign's
   machine-readable production plan and the input to future
   automation.
2. **Snapper campaign view**: one campaign shown at a time, selected
   by tab, defaulting to the current campaign; the window opens at the
   campaign's first recorded delivery. The primary display is the
   arrivals quilt: stacked per-configuration daily arrivals, one color
   per PC, production bursts visible as attributed bumps. Cumulative
   series (absolute, and fraction of target where events are known)
   are a secondary family, off by default. Curves draw only from the
   daily registered-basis record, so the plotted series ends at the
   last complete day and never mixes bases with the live
   placement-checked component, which feeds the cut cards. Lens
   factorization applies over the quilt as the noise-reduction knob. The cut card provides the drilldown, from a group to
   its samples with completion to each sample's PCS dataset page. The
   lens and campaign are carried in the URL, so a group's view is a
   bookmarkable link. The view is a report-page preset over the existing scope
   machinery.
3. **Ops dashboard embed**: the existing snapper embed carrying the
   delivery curve family, click-through to the campaign view.

Candidates not committed in this plan: delivered-dataset arcs as
episodic lanes (a tile per dataset from first arrival to fully
placed), and the disk/tape/capacity presentation.

## Delivery sequence

Each step is a functional delivery and release boundary:

1. PCS: the expected-events field and the campaign plan list, making
   target curation live; then the 26.06/26.07 target curation pass.
2. The delivery component: publisher on the catalog-sync chain, plus
   the backfill builder producing the 26.06/26.07 daily record.
3. The Snapper campaign view: lens projection in the provider, the
   campaign tabs and preset, the delivery cut card.
4. The ops dashboard embed placement.

Categorization assignment (extension 3) can land any time after step
2; lenses apply retroactively by construction.
