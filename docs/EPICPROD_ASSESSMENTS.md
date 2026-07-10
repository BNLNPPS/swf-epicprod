# ePIC Production Campaign Assessments

Scheduled LLM (large language model) assessments of production campaigns:
a nightly operational assessment and a weekly trend assessment of each
producing campaign, generated automatically, registered as AI assessments
on the campaign, and distributed through the production channels. The
assessments concentrate operator attention — what changed, what needs
action — and build the artifact record that the campaign dashboard and
later assessments consume.

This is a design document, written 2026-07-10 at the start of the v38
cycle; nothing below is implemented yet. It builds on
[EPICPROD_LLM_OPERATIONS.md](EPICPROD_LLM_OPERATIONS.md) (the corun-ai
execution architecture), [EPICPROD_NARRATIVES.md](EPICPROD_NARRATIVES.md)
(the campaign narratives that define what progress is measured against),
[ACTION_STREAM.md](https://github.com/BNLNPPS/swf-monitor/blob/main/docs/ACTION_STREAM.md) (the structured
action record), and the AI assessment mechanism in
[EPICPROD_OPS.md](EPICPROD_OPS.md#ai-assessments).

## Architecture

corun-ai executes the assessments. It holds the LLM credentials, model
selection, prompt templates, and output schema as its own configuration,
and stores the resulting artifacts with model and prompt provenance;
swf-monitor holds no LLM credential and makes no LLM call. The
assessment runs as a corun-ai work item — the scheduled counterpart of
the on-request codoc-ai analyses.

- **Trigger.** A scheduled script on the production host posts to a
  corun-ai REST endpoint to create the work item, carrying only the
  subject reference: campaign, assessment kind (`nightly` or `weekly`),
  and evidence window. Cadence is held in SysConfig; defaults are
  nightly at 03:45 ET, after the 02:15 catalog-sync chain has refreshed
  the production state it will assess, and weekly on Monday at 06:00 ET.
  The trigger records an action-stream event.
- **Context.** The corun-ai worker gathers its evidence as an MCP
  (Model Context Protocol) client of swf-monitor, the same access path
  the DISpatcher bot uses. Campaign narratives and prior assessments
  are corun-ai artifacts and are read directly. Production state —
  progress, arrivals, dispositions, alarm state, action-stream
  activity — is served by a campaign-status MCP tool (new; see
  Analytics Library below) alongside the existing `epicprod_*`,
  `panda_*`, and `pcs_*` tools.
- **Registration.** The worker registers the result through
  `epic_register_ai_assessment` — the write path whose intended callers
  already include automated production assessors — with subject type
  `campaign`, metadata `assessment_kind` and `origin: scheduled`, and a
  structured verdict. Registration logs an epicprod action whose
  sublevel rises with a non-`ok` verdict, so an assessment that calls
  for attention reaches the live stream and the epicprod-live
  Mattermost channel without additional machinery.

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

## The Assessment Artifact

Each assessment is one artifact with a deterministic shape, carrying a
`schema_version`:

- **Structured block** — the machine-readable result: verdict, per-axis
  status, key metrics and deltas with references to the objects behind
  them, and the top issues. The dashboard and downstream automation
  consume this block only.
- **Prose block** — the bounded interpretation, for humans reading the
  assessment itself.
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

- **The model interprets; it does not calculate.** Every number in the
  artifact originates in the supplied evidence, and the harness verifies
  emitted numbers against supplied ones. Arithmetic is the analytics
  library's job.
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

The harness — the deterministic script wrapping the LLM call on the
corun-ai side — guides the operation and cleans up after it:

- Assembles the evidence, applies the template and schema from corun-ai
  section configuration (versioned, with provenance, following the
  section-carried prompt convention).
- Validates the output against the schema, with a bounded re-prompt on
  mismatch.
- Resolves every scheduled slot to a visible outcome. One of: a valid
  artifact; a quarantined malformed artifact — marked as malformed,
  excluded from dashboard data paths and from later assessment context,
  raw output retained for diagnosis; or a failure record carrying the
  error. A slot that never fills raises a freshness alarm, following
  the catalog-sync freshness pattern. A malformed result is never
  dropped: the display shows the quarantined artifact or a red failure
  state.
- Enforces one artifact per (campaign, kind, date); a rerun replaces
  its predecessor. Nightly artifacts roll up under retention rules; the
  dashboard reads the latest N.

## Cadence

The nightly assessment is short and operational, and runs even when
little has changed. A quiet entry is inexpensive and is itself
information; more importantly, the unbroken nightly sequence is what
makes trend statements in later assessments verifiable, since the
assessor's trend awareness is its own prior artifacts read back as
context. The weekly assessment measures the campaign against its
narrative's stated goals over a seven-day window; it uses the same
schema with a larger prose budget, since trend interpretation is where
the model's judgment carries the most value.

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

Each step is a functional delivery and a release boundary:

1. Campaign analytics library, the campaign-status rollup service, and
   the `epicprod_campaign_status` MCP tool.
2. The corun-ai assessment operation — template, schema, work type,
   harness — the scheduled trigger, and registration verdict handling.
3. Surfacing: assessment filters, the producing-tab verdict badge, and
   narration distribution.
