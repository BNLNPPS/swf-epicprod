# epicprod Campaign Narratives

Narrative documents are the curated context that epicprod's AI assessors and
reporting load before reasoning about production state: what a campaign is
for, what should be running, what changed, and the standing facts of how
production operates. They are extracted once from raw material — meeting
presentations, planning discussions, release notes — so that no runtime
consumer ever re-derives context from slides or dialog.

Narratives are AI-authored **products** and live in corun-ai (created and
read through its REST interface), alongside assessments and reports. The
**rules** that govern them — this document, the naming grammar, the code
that enforces both — live in git. A narrative cites its governing
conventions; it never carries the master copy of them.

## Document classes

**General narrative** — the standing context: the dataset-request provenance
chain, resource landscape, priority and readiness semantics, naming
conventions, operational posture. Revised as a dated series; each revision
is a full re-baseline.

**Campaign narrative** — the evolving specification of one campaign:
purpose, software identity (container, geometry, reconstruction tags), the
dataset matrix with priorities and dispositions, timeline expectations,
retirements and replacements. Drafted before the campaign, living during
it, its last version standing as the permanent record after.

Campaign narratives cite the general narrative and never restate its
content. Consumers always load the current version of each.

## Naming

```
campaign_general_YYYYMMDD          general series, dated
campaign_<campaign>                one living page per campaign, e.g. campaign_26.07.X
```

`<campaign>` is the campaign's canonical name as PCS records it (the bare
version, e.g. `26.07.X`) — never an approximation of it. The name is the
series identity, carried in the corun page's `data.name`; the page title
is the human title.

## Lifecycle: versions

A narrative is a living description — evolved by time, corrected and
elaborated by experts — so its lifecycle is corun-ai's native versioning,
not a lock. Every save creates an immutable version; the current version
is what consumers load; every past version is retrievable forever.

- **Citation rule**: an assessment cites a narrative by *name and
  version* ("campaign_26.07.X v5"). Versions are immutable, so the cited
  reference always resolves to the same content — the auditability the
  earlier draft→locked design sought, provided natively.
- **Editing** is a signed act: each save stamps the editor and logs
  `narrative_edited`; the version history (bottom of the document detail
  page) shows version, date, author, and size, with any version viewable.
- **Comments** (corun-ai threads, on the list entries and the detail
  page) are the non-intrusive contribution path beside editing; posting
  logs `narrative_commented`.

The PCS tag draft → locked lifecycle is unaffected — that lock is
load-bearing for reproducibility of produced data.

## Content rules

- **Complete, not delta.** A consumer loads exactly one document per
  series. A "changes since previous revision" section inside the document
  serves human readers; the rest of the document stands alone.
- **Sources are part of the document, with links.** Every narrative
  carries a linked source list — meetings, presentations, records — so
  any claim traces to raw material.
- **One resolver.** "Current" (highest date for the general series, the
  campaign's page for a campaign) is implemented once, in the service
  layer, and shared by every consumer; it is not a convention each client
  re-implements.

## Relationship to the catalog

The campaign narrative's disposition content — what continues, holds,
retires, and what replaces what — is carried structurally by the dataset
`propagation` state and `replaced_by` reference (see
[PCS.md](PCS.md#datasets)). The narrative states the intent and the why;
the catalog fields make it computable. The procedurally generated campaign
summary (the per-family beam × Q² matrix with readiness and disposition)
accompanies the narrative as a generated artifact, not hand-authored
content.

## Related

- [PCS.md](PCS.md) — dataset identity, campaign editions, propagation.
- [EPICPROD_TASK_CATALOG.md](EPICPROD_TASK_CATALOG.md) — the catalog the
  narratives describe.
- [ACTION_STREAM.md](https://github.com/BNLNPPS/swf-monitor/blob/main/docs/ACTION_STREAM.md) — the action
  record assessors reason over, against narrative context.
- [EPICPROD_LLM_OPERATIONS.md](EPICPROD_LLM_OPERATIONS.md) — the corun-ai
  document store and REST interface.
