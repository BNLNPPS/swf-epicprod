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

## Backfill

The past record is reconstructible: JLab Rucio file DIDs carry
registration times and sizes, and events/file is time-independent
configuration. Events and bytes arrival series for 26.06 and 26.07
therefore reconstruct exactly, at daily resolution, keyed to PCs
through the DID–PCS mapping. Backfilled snaps are stamped with a
`backfill-v1` capture policy: reconstructed evidence, explicitly
distinguishable from observed evidence. The placement split does not
reconstruct (no lock-state history) and is deferred with the
disk/tape metric.

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
