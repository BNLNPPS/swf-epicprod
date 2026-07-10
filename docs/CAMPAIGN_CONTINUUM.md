# Campaign Continuum

Design for a catalog in which production is one continuous story and
every campaign carries the same curated representation. Companion to
[PCS.md](PCS.md) (the dataset identity model),
[PCS_DATASET_REQUEST_WORKFLOW.md](PCS_DATASET_REQUEST_WORKFLOW.md) (the
request workflow), and [EPICPROD_TASK_CATALOG.md](EPICPROD_TASK_CATALOG.md)
(the catalog surface).

## The problem

The catalog was built current-first. The current campaign received the
curated, actionable representation — tag-composed identities, request
context, filters, propagation, AI proposal surfaces, submission lifecycle —
while past was tacked on as a read-only output table and future as a
placeholder. Lifecycle labels therefore select between representations of
different quality, and every transition is a demotion: rotating current to
last moves curated material into a neglected form. The 26.06.0 interim
campaign made the defect concrete — it kept producing for a month while the
catalog, having filed it as past, stopped representing it.

A second binding error compounds the first: requests are bound to one
campaign (the CSV import attaches request rows to current), but a request
points at physics, and its fulfillment crosses campaign boundaries — the
requests bound to 26.05.0 are being fulfilled by 26.06.0's production.

## Principles

1. **One curated representation for every campaign.** Identity (tags,
   composed names, sample variants), request linkage, outputs, propagation,
   and AI surfaces exist for past, last, current, producing, and future
   campaigns alike. Missing data is an empty field, never a lesser schema.
2. **Lifecycle is a phase attribute, not a quality attribute.** The phase
   gates what an operator can do — never what the catalog knows or shows.
3. **Requests bind to physics, not to a campaign.** The request's anchor is
   the **physics configuration** — physics tag + evgen tag (+ background
   tag) + sample variant, campaign-invariant (PCS.md; distinct from a
   production config, the execution workflow settings) — matched on
   semantic filter fields, never on paths. A campaign realizes a physics
   configuration as an **edition**, adding its own simu/reco tags,
   detector config, and version segment. Editions fulfill the request;
   fulfillment status is an aggregation over editions.
4. **Rotation is a policy flip.** Promoting a campaign changes affordances
   and sweep targets; it moves no data and demotes no representation.
5. **Instancing is the continuum's forward mechanism.** A new campaign's
   editions are minted from the predecessor's continuing physics
   configurations (their dispositions consumed), merging with whatever
   ingest has already observed.

## Requests over physics configurations

Today a request lives as CSV context on a `ProdTask` bound to one campaign.
In the continuum model:

- The request's identity is its filter-field block (detector, beam,
  physics, Q² range, species/energy) — the same axes the Rucio output
  matcher already uses. That block resolves to a physics configuration.
- A physics configuration's fulfillment is the set of its editions and
  their outputs: requested → produced-in-26.05 → reproduced-in-26.06 →
  planned for 26.07 is one physics configuration's timeline, not four
  unrelated records.
- The edition keeps what is genuinely per-campaign: the campaign's
  simu/reco tags, submission state, production config, outputs,
  edition-level disposition (semantically a physics-configuration-level
  decision — "does this get an edition in the next campaign" — stored on
  the current edition).

No new entity is required: the physics configuration remains a derived key (PCS.md), and
request context migrates from "rows owned by the current campaign" to
"rows resolved to physics configurations, projected onto whichever editions exist."

*(Implemented.)* The catalog CSV import writes one `ProdRequest` per row,
idempotent on the row key, and binds it to its physics configuration through
`data['physics_config_anchor']` — the composed name of the edition the request
was recorded against, a name reference in the same convention as
`replaced_by`. Production-team triage fields and status are never touched by
re-import. `pc_request_projection` (`pcs/services.py`) is the read path: any
edition resolving to the same configuration key carries the request, so the
catalog task rows, the compose detail, the physics-configuration view, and the
edition data page all reach requests through the configuration — requests
point to configurations, configurations list their requests, tasks reach
requests only via their configuration. The edition data page is the request's
home and renders it in full; request links elsewhere land there. Task request
columns (`REQUEST_TO_TASK_COPY_FIELDS`) are creation-time seeds: re-imports
refresh the request row and no longer re-stamp the task.

## The physics-configuration view

The tabs answer "what is campaign X?" The physics-configuration view
answers the production team's other standing question: "where does each
piece of physics stand?" One row per physics configuration; its editions
as sub-information — requested, produced per campaign
(files/size/completeness), disposition, proposals. This is the natural
home of cross-campaign judgment: propagation decisions, energy
migrations, retirement-with-replacement all read configuration-first,
and request matching lands here (request → physics configuration;
campaigns map to editions). The campaign tabs and the
physics-configuration view are two projections of the same records —
campaign-first and physics-first. They are separate views, and different
in kind: the physics-configuration view is not another PCS working
display — it has no compose function and no editing machinery. It is
optimized for presentation: physics first, showing physics fulfillment
through time — through campaigns — with each configuration's editions
laid out along the campaign axis. Its own page, its own physics-axis
filters (process, generator, beams, species, Q²), campaign as a column
rather than a tab, cross-links to and from the catalog for anything
actionable. It maps directly onto the production team's planning tables
(configuration blocks with a beam × Q² matrix), which become an export
of this view.

## One view, phase-gated affordances

Every campaign tab renders the same curated task list. The phase decides
the affordances:

| Phase | Affordances beyond browse/filter |
|---|---|
| past | read-mostly; ingest keeps outputs current |
| last | comparison surfaces (Rucio timeline) |
| current | full working set: edit, submit, propagation, AI decisions |
| producing (derived) | arrivals strip; promote rotation |
| future | dispositions, instancing, planning |

The past view's genuine extras — stage facets, release navigation, the
arrivals timeline — become additions available on any campaign with the
underlying data, not the property of a separate template.

## Instancing and the 26.06.0 rehearsal

Instancing (create-next-campaign) mints the successor campaign's editions
from the predecessor's physics configurations whose dispositions say `continue`,
carrying request context forward and consuming `hold`/`final`/`replaced_by`
decisions. Merge rule: where ingest already observed an edition (26.06.0's
131 identities exist with correct version segments and physics tags), the
minted task binds to the existing dataset row — never a duplicate; the
observed row becomes the task's output-bearing edition, and refinement
(anchor evgen/simu/reco tags, sample names) is curation work on the same
row.

Populating 26.06.0 as a working catalog is this mechanism's first
application, and the dress rehearsal for 26.07: same code path, real
production to reconcile against. It ships as a reviewable operator action —
the mechanism proposes, a human fires it — with a dry-run listing of what
would be minted, merged, and skipped.

The Future tab gives the next campaign the same treatment the moment it is
detected — an existing future-lifecycle row, or the version named by pending
disposition batches before any row exists. The plan tolerates the missing
row (every continuing configuration classifies as mint), so the tab lays out
what a population would do; firing it creates the campaign row (lifecycle
`future`) and populates it, and the normal rotation takes over from there.
The plan sources from the newest producing campaign when one is ahead of
current (it will be current by population time); otherwise from current.
Dispositions are resolved configuration-wide, whatever campaign's edition
carries them — a deliberate hold or final on any edition decides the
configuration (the default `continue` is non-information; hold and final
disagreeing is a conflict for a human) — so decisions stamped on the
current campaign's editions govern a plan sourced from a newer one.
With that the catalog perpetuates itself: each campaign's successor is
planned, populated, promoted, and retired inside the catalog.

## Migration path

1. **Continuum instancing** (the 26.06.0 rehearsal): mint-and-merge, with
   the CSV-import version-segment naming fix riding along. *(Implemented —
   `pcs/instancing.py`; 26.06.0 populated 2026-07-09.)*
2. **Unified view**: the campaign tabs converge on the curated task-list
   rendering with the phase-affordance matrix; the past template's extras
   fold in as data-gated additions. *(Implemented for every single-campaign
   view — producing, last, single-release past; multi-campaign aggregates
   keep the outputs table, their genuine role.)*
3. **Requests over physics configurations**: request context resolves to
   physics configurations and projects onto editions; the CSV import stops
   binding requests to the current campaign. *(Implemented — see "Requests
   over physics configurations" above.)*
4. **The physics-configuration view**: the inverted, datasets-first
   projection. *(Implemented — `/pcs/physics/` and the per-configuration
   Rucio-data-per-campaign page; firsthand reconciliation in
   `pcs/reconcile.py` keeps producing campaigns' records current.)*

Each step is independently shippable and none reshapes stored data
destructively; steps 3 and 4 share the configuration-resolution machinery.

## Open questions

- Fulfillment semantics: when is a request "fulfilled" — any edition
  produced, the newest edition produced, or a per-request statement of
  which campaigns count?
- Configuration view scale: one row per physics configuration is ~500 rows today; the view needs
  the same filter machinery as the task list from day one.
- Request identity for non-CSV intake (questionnaire, future DISpatcher
  dialog): the filter-field block is shared; intake surfaces converge on
  it (PCS_DATASET_REQUEST_WORKFLOW.md).
