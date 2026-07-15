# ePIC Production Dashboard

The master production dashboard: one operations surface composing the
production system's existing pages into a grid of panels. Each panel is
a bounded excerpt of a page that already exists — the same records, the
same links — so the dashboard is an index over the system, and the
existing pages remain the drill-down surfaces. Version 1 is pure
composition: every panel renders from a data service already in
production, and the dashboard adds no new computation.

This is the plan of record for version 1, written 2026-07-14. It builds
on the campaign analytics library and rollup
([EPICPROD_ASSESSMENTS.md](EPICPROD_ASSESSMENTS.md), whose structured
blocks were designed as dashboard inputs), the action stream
([ACTION_STREAM.md](https://github.com/BNLNPPS/swf-monitor/blob/main/docs/ACTION_STREAM.md)),
the browser-push machinery
([SSE_PUSH.md](https://github.com/BNLNPPS/swf-monitor/blob/main/docs/SSE_PUSH.md)),
and the AI-attribution convention
([AI_PROPOSALS.md](https://github.com/BNLNPPS/swf-monitor/blob/main/docs/AI_PROPOSALS.md)).

## Placement

The dashboard speaks production vocabulary and is new work, so it
lives in this repository per
[ARCHITECTURE_MAP.md](ARCHITECTURE_MAP.md): the view, templates, and
panel providers live in the `pcs` application and ship into the
monitor as installed code. The production home page (`/prod/`) gains a
discreet tab pair at top left:

- **Nav** — the existing workflow hub, the default tab.
- **Ops** — the dashboard.

The tab control itself is navigation, a thin monitor-side adapter; the
Ops tab content is the production dashboard view.

## Panels

The default order follows the production flow. Each panel shows at most
a fixed number of entries (default 8, a template constant) and links to
its full page; the bound keeps panel heights near-uniform and the grid
legible.

| Panel | Content | Source | Links to |
|---|---|---|---|
| Requests | Latest production requests, one line each, reverse time | PCS request records | Request list |
| Physics configurations | Latest production configurations, one line each, reverse time | PCS configuration records | Prod configs |
| Campaigns | Few-line overview of the current campaign and each producing campaign: tasks with processing, files and volume, placement completeness | Campaign progress rollup (analytics library); producing is the derived arrivals-window status | Campaign catalog, campaign progress |
| PanDA activity | Window distillation: jobs by state, final-failure rate, top error codes, core-hours | `panda_health` analytics member and resource-usage queries | PanDA activity, site usage, alarms |
| Science data arrivals | Latest JLab Rucio file arrivals at dataset granularity | Recorded arrival sweeps and the dataset first-arrival timeline | Campaign progress |
| AI assessments | Latest daily report per assessed campaign: report title and narration | Assessment registry (AI content) | The report page |
| AI proposals | Outstanding proposals awaiting decision | Proposal ledger | Proposals page |
| epicprod live | Most recent live-axis production actions, reverse time | Action stream | Live feed (logs page filter) |

The two AI panels use the purple-on-lavender AI-attribution
convention: an `ai-fill` container with an `ai-attr-text` title.

The live panel renders its recent entries at page load and carries an
"updated N s ago" indicator beside its title; its cache holds the
content within 30 seconds of current. The panel opens no stream — a
viewer who needs real-time delivery follows the panel's link to the
live feed. (An on-demand stream control was considered and rejected:
the indicator plus the link cover the need without a held connection.)

## Presentation rules

- One-liner discipline everywhere: timestamp, subject, a single line,
  a link. Panel content is drawn from the same queries as the full
  pages it excerpts.
- Factual content only: no verdict badges and no status coloring in
  version 1. Assessment one-liners carry the report's narration text.
- Text follows the monitor's readability defaults; no small or
  low-contrast rendering.

## Serving and freshness

Panel content is user-independent and cached per panel (Redis in
production). Serving never waits on freshness: the page renders
instantly from whatever is cached, however old, and each panel then
revalidates itself — immediately after load and every 30 seconds for
the live panel, every 120 seconds for the rest — through a per-panel
refresh endpoint that rebuilds only when the cached copy is older than
the panel's freshness window. The live panel shows its age beside its
title; a counter climbing past its window is the visible signal that
refresh has stopped. The only synchronous build is a cold cache.

## Grid and interaction

Panels are Bootstrap cards in a responsive `row-cols` grid: one column
on narrow windows, two at medium width, three at wide. Layout comes
from stock framework classes; no bespoke layout CSS.

Users reorder panels by dragging, via SortableJS. The persisted state
is the ordered list of panel identifiers, which reflows correctly
whatever the current column count. Panels are not resizable.

## User state

Dashboard preferences are stored per account in the platform
`UserPreference` record (username-keyed JSON):

```json
{"home_tab": "nav | ops", "panel_order": ["requests", "..."]}
```

One authenticated endpoint saves the record. Anonymous viewers get the
defaults — Nav tab, flow order — and the page notes that logging in
preserves layout choices. Browser-local storage is not used: state
saved server-side follows the user across machines.

## Summary strip

A single factual line on the Nav tab — current campaign, producing
campaigns, open alarm count, nearest credential expiry — built last,
once the panels are in place. No status coloring in version 1.

## Build sequence

Each step is a functional delivery and a release boundary:

1. Ops tab with server-rendered panels in the default order, bounded
   and linked.
2. Drag reordering and per-account persistence of panel order and tab
   choice.
3. The live panel's updated-age indicator.
4. The summary strip on the Nav tab.
