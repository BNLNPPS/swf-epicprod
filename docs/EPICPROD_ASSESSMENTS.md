# ePIC Production Campaign Assessments

Scheduled LLM (large language model) assessments of production campaigns:
a daily operational assessment and a weekly trend assessment of each
producing campaign, generated automatically, registered as AI assessments
on the campaign, and distributed through the production channels. The
assessments concentrate operator attention — what changed, what needs
action — and build the artifact record that the campaign dashboard and
later assessments consume.

This is a design document, written 2026-07-10 at the start of the v38
cycle and implemented in the v39 cycle;
[EPICPROD_ASSESSMENTS_V1.md](EPICPROD_ASSESSMENTS_V1.md) carries the
as-built contract. It builds on
[EPICPROD_LLM_OPERATIONS.md](EPICPROD_LLM_OPERATIONS.md) (the corun-ai
execution architecture), [EPICPROD_NARRATIVES.md](EPICPROD_NARRATIVES.md)
(the campaign narratives that define what progress is measured against),
[ACTION_STREAM.md](https://github.com/BNLNPPS/swf-monitor/blob/main/docs/ACTION_STREAM.md) (the structured
action record), and the AI assessment mechanism in
[EPICPROD_OPS.md](EPICPROD_OPS.md#ai-assessments).

## Architecture

corun-ai executes the model run — the invariant piece — as a generic
work item, the scheduled counterpart of the on-request codoc-ai
analyses. It holds the LLM credentials and stores the resulting
artifacts with model and prompt provenance; swf-monitor holds no LLM
credential and makes no LLM call. The deterministic envelope around the
run — evidence assembly before it, enforcement after it — is
production-side epicprod code, because what enters the model and what is
accepted out are app logic under intense iteration; the template and
output schema are epicprod content stored versioned in corun over REST.
corun carries no epicprod-specific code and no new models. (Placement
settled 2026-07-12; execution rides the codoc job queue —
scheduled-and-patient work — with a wrangle-ai-based executor foreseen
for the event-driven-and-waiting class, both behind the same REST
shape. [EPICPROD_ASSESSMENTS_V1.md](EPICPROD_ASSESSMENTS_V1.md) carries
the concrete contract.)

- **Trigger and evidence.** A scheduled script on the production host —
  the harness front end — assembles the evidence deterministically (the
  campaign-status rollup and landscape summaries, campaign and general
  narratives, and production-owned status history; generated assessments are
  not evidence). It stores the complete bundle as a Page in the hidden
  `epicprod.assessment.bundle` corun section, then submits the run with the
  bundle and subject reference — campaign, assessment kind (`daily` or
  `weekly`), evidence window — as prompt content. The hidden bundle is never
  registered or presented as an assessment; the human report links its direct
  Page URL. During the
  run the model additionally holds the swf-monitor MCP (Model Context
  Protocol) toolset, the same access path the DISpatcher bot uses, for
  drill-down beyond the bundle. Production state is served by the
  campaign-status MCP/REST rollup (see Analytics Library below)
  alongside the existing `epicprod_*`, `panda_*`, and `pcs_*` tools.
  Cadence defaults: the daily at 03:45 ET, after the 02:47 catalog-sync
  chain has refreshed the production state being assessed, and the
  weekly on Monday at 06:00 ET. The trigger records an action-stream event.
- **Registration.** The production-side completion handler — fed by
  corun's job-completion callback — validates the artifact and registers
  it through `epic_register_ai_assessment` — the write path whose
  intended callers already include automated production assessors — with
  subject type `campaign`, metadata `assessment_kind` and
  `origin: scheduled`, and a structured verdict. Registration logs an
  epicprod action whose sublevel rises with a non-`ok` verdict, so an
  assessment that calls for attention reaches the live stream and the
  epicprod-live Mattermost channel without additional machinery.

## Campaign Analytics Library

The analytics library is the deterministic computation layer beneath
the assessments: a set of versioned algorithms, each
producing a data block (series and aggregates, JSON) and a rendering
(plot or table). Each run is recorded, and each artifact carries its
computation time and input window. Some members formalize analytics
that already exist — the Rucio arrivals timeline, the campaign progress
rollup — and the set grows with the dashboard: failure-rate series,
throughput, disposition mix.

The library serves three consumers from one computation:

| Consumer | Use |
|---|---|
| Campaign dashboard | Renders the data blocks and plots directly. |
| Assessment worker | Receives the data blocks (and renderings) as evidence. |
| `epicprod_campaign_status` MCP tool | Serves the rollup to any MCP client. |

The assessment harness brings the library current before assessing.
Staleness is visible: an assessment generated against analytics older
than its evidence window is marked as such rather than silently
accepted.

Normal production refresh records campaign-status snapshots independently of
assessment runs. Daily comparisons select the recorded snapshot closest to 24
hours before the current state and report the actual elapsed interval.

## The Assessment Artifact

Each assessment is one artifact with a deterministic shape, carrying a
`schema_version`:

- **Evidence bundle** — the complete original production input, retained as a
  hidden corun Page and linked explicitly for audit. Its body is a
  deterministic Markdown review document, sectioned by evidence artifact and
  primarily tabular; the exact machine object is retained in Page metadata.
- **Structured judgment** — the model result: verdict, per-axis status,
  conclusions, software findings, issues, outlook, citations, and generation
  provenance. The model does not reproduce deterministic metrics.
- **Human report** — assembled by production code. The facts appropriate for
  an expert reader are rendered directly from the bundle; model judgment is
  inserted only in its named sections. Bulky raw blocks remain behind the
  bundle link.
- **Investigation evidence** — a separate hidden, one-off corun Page produced
  after the run, containing the model's structured live-tool evidence records,
  exact contract artifact, and runner transcript. The human report links it;
  it is never registered as an assessment or reused as production evidence.
- **Narration** — a self-contained summary of a few sentences, written
  to stand alone without the charts: campaign, date, verdict, and the
  one or two things that matter. This single field is the payload for
  every thin delivery channel — the Mattermost publisher, email,
  mobile — so no channel needs its own generator.

The verdict vocabulary is `ok | attention | alarm`.

## Determinism Rules

The model is treated as an untrusted generator inside a deterministic
envelope. The template and schema define the contract; the harness
enforces it.

- **The model interprets; it does not own the facts.** Production code renders
  metrics, deltas, intervals, and evidence labels from the bundle. The harness
  validates the model's bundle ID, evidence timestamp, and narrative citation
  against the exact submitted artifact.
- **Verdict floor.** Mechanical criteria — final-failure rate,
  catalog-sync freshness, stalled arrivals — compute a minimum verdict
  before the model runs. The model may raise the severity with
  justification; it cannot lower it below the floor.
- **No chart narration.** The template directs the assessment at what
  the analytics do not state: correlation across signals (an arrivals
  dip, one site's error spike, and a queue alarm as one event rather
  than three), deviation from the narrative's stated intent, trend
  inflection, and the explicit call on what requires human action.
  Restating chart contents is excluded by the template.

## Harness Lifecycle

The harness — the deterministic envelope around the LLM call,
production-side epicprod code split between the submission front end and
the completion handler — guides the operation and cleans up after it:

- Assembles the evidence, applies the template and schema (epicprod
  content stored versioned in corun, with provenance, following the
  section-carried prompt convention).
- Validates the output against the schema, with a bounded re-prompt on
  mismatch.
- Resolves every scheduled slot to a visible outcome. One of: a valid
  artifact; a quarantined malformed artifact — marked as malformed,
  excluded from dashboard data paths and from later assessment context,
  raw output retained for diagnosis; or, for a run that fails before
  submitting its result (timeout, crash), a midstream salvage: the run
  is resubmitted once, and a salvage report is registered carrying the
  mechanical floor verdict, the model's unfinished narration recovered
  from the runner transcript, and an explicit statement that it is not
  a completed assessment, with the full transcript preserved as a
  linked crashed-run evidence page. A slot that never fills raises a
  freshness alarm, following the catalog-sync freshness pattern. A
  failed or malformed result is never dropped: the display shows the
  quarantined artifact or the salvage.
- Retains reruns as audit history without feeding generated reports back into
  later evidence.

## Cadence

The daily assessment is short and operational; its subject is the last
day's window — productivity, new problems, and current conditions — compared
with production analytics recorded closest to 24 hours earlier. It runs even
when little has changed: a quiet entry is itself information. The weekly
assessment is the standalone report: complete in
itself, re-baselining against production analytics recorded closest to seven
days earlier, it measures the campaign against its narrative's stated goals
over a seven-day window with the same schema and a larger prose budget, since
trend interpretation is where the model's judgment carries the most value.
Prior AI reports are not evidence and are not required. A quiet week rightly
reads much like the previous week's report.

## Dashboard Relationship

The campaign dashboard renders the analytics library directly; its
numbers do not depend on an assessment existing. Assessments supply the
judgment layer — the verdict, what changed, what needs action — and the
narration. The two are developed together: the structured block is
designed as dashboard input.

## Surfacing

- The AI assessments page gains filters: subject type, assessment kind,
  origin, verdict, campaign.
- The catalog's producing tab shows the latest verdict as a badge.
- The narration field is distributed through the epicprod-live
  Mattermost publisher; email delivery follows the alarm path.

## Implementation Plan

The concrete v1 plan — workstreams, schema, prompt templates, sequencing —
is [EPICPROD_ASSESSMENTS_V1.md](EPICPROD_ASSESSMENTS_V1.md) (2026-07-12).

Each step is a functional delivery and a release boundary:

1. Campaign analytics library, the campaign-status rollup service, and
   the `epicprod_campaign_status` MCP tool.
2. The assessment operation: corun's REST completed for autonomous use;
   the production-side harness — evidence-assembling trigger, template
   and schema bootstrapped into corun, completion handler — and
   registration verdict handling.
3. Surfacing: assessment filters, the producing-tab verdict badge, and
   narration distribution.
