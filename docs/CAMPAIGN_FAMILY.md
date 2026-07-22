# Campaign Families

The campaign is the family — the first two fields of the version name
(26.07). The third field is the patch level of the monthly software
release, nothing more: between `v26.07.0-stable` and `v26.07.1-stable`
the eic/containers history holds exactly two commits (EICrecon 1.39.1,
then 1.39.2 with hepmcmerger 2.4.0 — four spack pins). Campaign intent,
physics scope, energy matrix, narratives, lifecycle, and operator
attention are all family-level; the patch is software provenance, and
provenance is recorded per task and dataset regardless (names embed the
patch, `Dataset.detector_version` carries it, and each PanDA
association records its executed container).

PCS previously treated each patch as a first-class campaign: separate
catalog rows and tabs, separate lifecycle buttons, separate assessment
targets, separate narrative expectations. This document is the design
and plan of record (2026-07-22) for aligning PCS's campaign unit with
ePIC's real one.

## Model

- **Campaign row = family.** `Campaign.name` is the two-field family
  name (`26.07`). Lifecycle, arrivals, producing status, narratives,
  and assessments key on it.
- **Patch editions remain fully recorded** where they belong: dataset
  and task names embed the full version, `Dataset.detector_version`
  carries it as a field, produced Rucio paths stay patch-level, and
  `PandaTasks.metadata['executed']` records the container actually
  run. Nothing about dataset or task identity changes.
- **Editions surface as a filter**: the catalog gains an Edition facet
  over `Dataset.detector_version` within a campaign view.

## Data migration (one-off, dry-run default)

`scripts/migrate_campaign_families.py`:

1. Group existing rows by family. For each group, the lowest-named row
   is renamed to the family name and becomes the family row; sibling
   editions' `Dataset.campaign` and `ProdTask.campaign` FKs re-point to
   it (the only two Campaign FKs).
2. Merge `Campaign.data`: `arrivals` keeps the newest block;
   `past_summary` re-keys per edition
   (`{edition: {stage: totals}}`); `rucio_unmatched` refreshes on the
   next sync.
3. Lifecycle of the family row: the most-advanced member of the group.
4. Emptied edition rows are deleted. 22 rows become 16 families (the
   five 25.10.x rows collapse to one October campaign, as ePIC thinks
   of it).

## Code changes

- **Family derivation single source**: `campaign_family(name)` in
  `pcs.name_tokens` (the assessment bundle's private copy retires onto
  it).
- **Writers produce family rows**: direct-PanDA intake resolves
  `Campaign` by family of the parsed version; the past ingest keys its
  campaign row by family and its totals by edition; the arrivals sweep
  groups by family while keeping per-edition location detail.
- **Snapshot fetch per family**: editions enumerated from
  `Dataset.detector_version` (plus sweep-discovered arrivals); one
  fetch per edition path (`/RECO/26.07.0`, `/RECO/26.07.1`) lands in
  one family snapshot file — the snapshot format is already keyed by
  campaign path, and reconcile iterates those keys unchanged.
- **Version comparisons tolerate two-field names** (`_version_tuple`,
  intake lifecycle classification, the next-campaign hint).
- **Catalog**: tabs and lifecycle buttons operate on family rows with
  no mechanical change; the filter bar gains the Edition facet.
- **Assessments**: targets resolve family rows, so one daily and one
  weekly per family cover all editions; PanDA population discovery uses
  the family name prefix, which matches every edition's task names;
  narrative resolution is already family-level; slot keys, freshness,
  and registrations follow the campaign name with no code change.
- **Stale links**: the catalog's `?campaign=` resolution falls back to
  the family when an exact name misses, so stored patch-level links
  (old assessment subjects, bookmarks) keep resolving.

## Continuity notes

- Historical assessment registrations keep their patch-level
  `subject_key`; they are records, not references. Verdict-standing
  counters restart once under the family subject.
- Campaign-analytics snapshot history is name-keyed, so the first
  family-keyed run after migration has no baseline; deltas degrade for
  one cycle and the report states it, per the standing staleness rule.
- Narrative pages named `campaign_26.07.0` continue to resolve as the
  family narrative; new pages may use bare family names
  (`campaign_26.08`).
