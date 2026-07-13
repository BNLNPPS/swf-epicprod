# Campaign Assessments V1 — Implementation Plan

This plan concretizes [EPICPROD_ASSESSMENTS.md](EPICPROD_ASSESSMENTS.md) into
a first working version of the daily and weekly campaign reports: the hybrid
procedural / template / LLM design, delivered promptly and elaborated
thereafter. It is written 2026-07-12 as the plan of record for the v1 sprint
and updated the same day to the as-built state. The scheduled report kinds
are `daily` and `weekly` — the daily is produced by a night cron but is the
day's report, not a "nightly" (records registered before the rename carry
`assessment_kind: nightly` and are read as dailies).

Two workstreams, by ownership:

- **Production side** (this workspace: swf-epicprod, swf-monitor) — the
  analytics library, the campaign-status rollup and its MCP/REST surfaces,
  the mechanical verdict floor, the `campaign` assessment subject, and the
  whole assessment harness: basis assembly and submission at the front,
  enforcement and registration on completion. The harness is where the
  iteration lives, so it lives here.
- **corun-ai side** (ec2dev) — generic execution only: the LLM run is the
  invariant piece, and corun needs no new models and no epicprod-specific
  code. The ask is to complete the REST API so epicprod drives corun
  autonomously end to end. Self-contained for handoff.

The design authority remains EPICPROD_ASSESSMENTS.md; where v1 cuts a corner,
the cut is recorded here.

## V1 scope

In: the daily assessment end-to-end (trigger → evidence → LLM → validated
artifact → registration → live stream/Mattermost), the weekly assessment as
the same machinery with a seven-day window and a standalone-report brief,
and an analytics library whose v1 members wrap computations the system
already performs. The two kinds carry distinct roles: the daily reports the
window — productivity, new problems, deltas against the production analytics
snapshot closest to one reporting window prior, and the mechanical floor
alarming on the window rather than the lifetime; the weekly is the standalone
report, complete in itself and re-baselined against production state closest
to seven days earlier.

Deferred, deliberately: analytics renderings (plots) beyond what pages already
show — v1 blocks are data-only; the campaign dashboard; assessment-page
filters; the producing-tab verdict badge; retention rollup of nightly
artifacts; SysConfig-held cadence (v1 uses fixed cron times with a SysConfig
enable gate). Each deferral is elaboration, not rework: the artifact schema
and rollup are designed as the dashboard's inputs.

## Production side

### Analytics library (swf-epicprod, greenfield)

`swf_epicprod/analytics/` — the first greenfield production component born in
this repository per [ARCHITECTURE_MAP.md](ARCHITECTURE_MAP.md). Each member is
a versioned function `compute(campaign, window) -> block` returning

```json
{"member": "...", "schema_version": 1, "computed_at": "<iso8601>",
 "window": {"start": "...", "end": "..."}, "data": {...}}
```

V1 members formalize existing computations; no new credentialed sweeps:

| Member | Source | Data block |
|---|---|---|
| `campaign_progress` | cached progress snapshot (`refresh_campaign_progress_snapshot` / `load_campaign_progress_snapshot`) | per-task completion percent, file counts, expected jobs and source; aggregates: task count, tasks with processing, complete count, total files/bytes |
| `rucio_arrivals` | recorded JLab file-arrival sweeps + `campaign.data['arrivals']` + the precomputed dataset-first-arrival timeline | newly created file DIDs in each sweep's actual interval; last-arrival age; separate lifetime dataset-first-arrival context |
| `panda_health` | bounded PanDA summaries already used by progress + the `nfinalfailed`/`computed_finalfailurerate` family (`monitor_app/panda/api.py`), error rollup per `panda_error_summary` | job-state counts, final-failure rate, top error codes with counts, active/finished task counts |
| `disposition_mix` | dataset `propagation` fields + history (PCS.md § Datasets) | counts by disposition; flips within window with comments |
| `action_stream_activity` | AppLog `app_name='epicprod'` | actions by id and outcome in window; chain-step durations; catalog_sync freshness (age of last successful chain) |
| `system_status` | cached platform status rows (the System page's source, in-process) | overall status and reason, per-state counts, staleness |
| `credential_status` | latest `credential_expiry_check` action record | days left per credential |

### Rollup service, verdict floor, surfaces

`swf_epicprod/analytics/rollup.py` composes all members into one campaign
status document and computes the **mechanical verdict floor** —
`ok | attention | alarm` with reasons — before any model runs. V1 floor rules
(thresholds in SysConfig, present at defaults per the no-hidden-knobs rule):

| SysConfig key | Default | Rule |
|---|---|---|
| `assessment_enabled` | `true` | master gate for scheduled triggers |
| `assessment_ffail_attention` | `0.10` | final-failure rate ≥ → at least `attention` |
| `assessment_ffail_alarm` | `0.30` | final-failure rate ≥ → `alarm` |
| `assessment_sync_stale_hours` | `26` | catalog_sync older → at least `attention` |
| `assessment_arrivals_stall_days` | `2` | no arrivals while tasks incomplete → at least `attention` |

Credential warning/expiry reuses `CREDENTIAL_EXPIRY_WARN_DAYS`: warning →
`attention`, expired/missing → `alarm`.

Surfaces, peers over one service per the standing rule:

- MCP: `epicprod_campaign_status(campaign=None, window_days=1)` — campaign
  defaults to the producing campaign(s), else current. This is the tool the
  corun-ai worker calls for its evidence, and later the dashboard's source.
- REST: `GET /pcs/api/campaigns/status/?campaign=&window_days=` — same
  service function.
- History: the normal campaign-progress refresh records each computed status
  as a `campaign_analytics_snapshot` in the existing epicprod action record;
  `history_at=<ISO timestamp>` returns the snapshot closest to the requested
  time. This production-owned series, not a prior AI report or prompt, is the
  comparison basis.
- Evidence artifacts: each complete input bundle is a corun Page in the
  dedicated hidden `epicprod.assessment.bundle` section, with
  `data.ui_visible: false` and
  `artifact_type: campaign_assessment_evidence_bundle`. It is not registered
  as an assessment or shown in normal corun browsing. The published report
  links its direct Page URL explicitly. The Page is a deterministic human
  review surface: production facts, comparison basis, manifest, source
  freshness, narratives, and each analytics member are rendered as sectioned
  Markdown with tabular data. The exact machine object remains in the Page's
  data field for harness verification; it is not the human presentation.

The rollup carries each member's `computed_at` so the harness can mark
staleness rather than silently accept old evidence.

### `campaign` assessment subject (swf-monitor, baseline-v39)

`epic_register_ai_assessment` gains subject type `campaign` (key: campaign
name as PCS records it, e.g. `26.06.0`; label and URL: the catalog view of
that campaign). Registration logs an epicprod action whose sublevel rises
when `data.verdict` is not `ok`, so an assessment calling for attention
reaches the live stream and the epicprod-live Mattermost channel with no new
machinery.

### The assessment harness (production side)

The harness is epicprod code, all of it: what enters the model and what is
accepted out are app logic under intense iteration, so they live here.
corun runs the invariant piece — the model — and nothing else (see the
corun-ai section). The harness has a front end at submission and an
enforcement end on completion; `scripts/assessment-trigger.py` (standalone,
stdlib-only, per the standing tooling preference) is the front end and
grows with it.

**Front end (cron, per target campaign):**

1. reads the SysConfig gate through the status REST; exits quietly when
   disabled; resolves targets (producing first, plus current when
   distinct);
2. assembles the Task 1 basis deterministically: the campaign status
   rollup (one fetch carrying all seven analytics members — progress,
   PanDA health, arrivals, dispositions, action stream, system status,
   credentials — and the verdict floor), the production analytics snapshot
   closest to one complete reporting window earlier, plus the narratives (`campaign_<name>`
   current, latest `campaign_general_*`). Prior AI reports are not evidence
   and are not supplied. Identical calls every run,
   per-call outcomes recorded; a failed must-look — including an absent
   campaign narrative — marks the run degraded in the bundle itself. The
   must-look set is versioned configuration here.
3. stores the complete JSON artifact as a hidden corun bundle Page and then
   submits the run: the evidence bundle and run parameters `{campaign,
   kind, window_days, requested_by}` ARE the prompt content
   (`POST /api/v1/prompts/` into the assessment section), then the job
   (`POST /api/v1/jobs/` with the kind's definition —
   `campaign_assessment_daily` or `campaign_assessment_weekly`).
   The resulting Page group ID and URL are added to the submitted evidence,
   registration metadata, and rendered report; the Page content is the
   original production evidence before that self-locator is attached.
4. records an `assessment_triggered` action-stream event per run, outcome
   `error` on any failure — a slot that never fills must be visible.

**The run (corun, invariant):** the versioned SystemPrompt establishes a
senior ePIC production and operations expert writing for expert peers. Before
assessment it loads the user profile, general/SWF worker guidance, and
documentation-writing guidance from TJAI. Its read-only investigation surface
is SWF Testbed (PanDA, PCS, epicprod, and the locally credentialed JLab and BNL
Rucio catalogs), XRootD, LXR, and a separately fenced GitHub service exposing
its complete read-only surface. Rucio credentials and the BNL proxy remain on
swf-testbed; corun consumes the prefixed `jlab_rucio_*` and `bnl_rucio_*`
tools through the authenticated SWF MCP service.
The prompt describes the knowledge held by each service,
the evidence layer it represents, and the investigation routes connecting
them. The bundle is the starting record; the model decides which material
signals warrant drill-down and returns bounded structured judgment. It does
not rewrite the deterministic facts or compose the Markdown report.

The daily is a focused operational delta report: activity, advancement,
material changes, and present attention. The
weekly is a standalone synthesis: campaign state, the week's production,
software/release state, responsibilities, and outlook. Both finish with a
substantive `### Generation report` naming context and evidence consulted,
actual tools and contributions, conflicts and failures, unavailable evidence,
and the resulting confidence. Production code renders that final section and
nothing follows it.

**Enforcement end (completion callback → prod-ops agent handler):** the
corun completion callback already lands at swf-monitor; a handler:

- fetches the result page and the run record (status, stderr) over REST;
- validates the model's structured judgment against the exact schema and
  rejects any model-written remainder; on failure, one bounded repair run receives the exact
  validation failures and prior output; a second
  failure quarantines the artifact (marked malformed, raw output
  retained, excluded from later context and from the assessments page)
  or records the failure — every scheduled slot resolves to a visible
  outcome;
- renders the report's fact tables directly from the submitted bundle and
  inserts model judgment only in the assessment, software, issue, outlook,
  and generation sections;
- enforces the floor: the artifact's verdict may exceed the bundle's
  floor with justification, never fall below it;
- stores the harness manifest, per-call outcomes, run timing, and enforcement
  state and immutable bundle reference as registration metadata;
- retains reruns as audit history but never feeds generated reports back as
  production evidence;
- registers: `epic_register_ai_assessment(subject_type='campaign',
  subject_key=<campaign>, ...)`, titled with the report's H1 (markdown
  links flattened to text — the assessments page leads with the
  title), with metadata `assessment_kind`,
  `origin: 'scheduled'`, `verdict`, `schema_version`, narrative citation
  (name + version), the structured block, and model/prompt provenance —
  the non-ok-verdict live-stream raise and the Mattermost relay follow
  from existing machinery.

Cron (wenauseic), after the 02:15 catalog_sync chain has refreshed the state
being assessed:

```
45 3 * * *  daily, every producing/current campaign
 0 6 * * 1  weekly, same targets, window_days=7
```

corun-ai URL, token, assessment section name, and the per-kind
definition ids (`CORUN_ASSESSMENT_DEFINITION_DAILY` / `_WEEKLY`; the
legacy `_NIGHTLY` value remains honored) live in `production.env`.

### Surfacing (v1 minimum)

The assessment renders where campaign AI content already renders (the AI
pages); the live stream and Mattermost carry the narration through the
registration action and the existing corun-ai job-completion callback →
pandabot relay. Badges, filters, and the dashboard are the elaboration phase.

## corun-ai side (ec2dev) — self-contained handoff

The assessment is **codoc-shaped work**: much input material and tools,
reasoned over — there is no single observation target (argus-ai, the
target-pointed probe design, is design-only as of 2026-07-12 and is not
this assessment's shape). Decided 2026-07-12 with the operator: **v1 rides
the codoc job queue** — the path codoc exercises daily. The queue serves
scheduled-and-patient work, which the nightly and weekly assessments are; a
wrangle-ai-based executor serves event-driven-and-waiting work (the future
on-demand assessment), and both present the same REST shape, so a later
move does not touch the production side.

**corun stays generic.** The LLM run is the invariant piece; everything
before it (what enters the model) and after it (what is accepted out) is
epicprod app logic under intense iteration and lives on the production
side — see The assessment harness. corun needs **no new models and no
epicprod-specific code**. The ask is to complete the REST API so a client
drives corun autonomously end to end — define, submit, monitor, retrieve —
with no human hands on corun configuration:

1. **Definitions**: `POST`/`PATCH /api/v1/definitions/` — create and
   update a JobDefinition (model, effort, mcp_tools, system-prompt group,
   timeout, status). GET exists today.
2. **System prompts**: `POST` + `GET /api/v1/system-prompts/` — create and
   version the SystemPrompt a definition references. No system-prompt REST
   exists today.
3. **Sections**: `POST /api/v1/sections/` — one-time creations, same
   principle. GET exists today.
4. **Run-outcome completeness**: `GET /api/v1/jobs/<id>/` exposing the
   run's error and stderr/output (the worker already captures them), or a
   `/jobs/<id>/log/` endpoint — failures must be machine-readable.
5. **MCP registry**: register TJAI, SWF Testbed, XRootD, LXR, and a separately
   fenced read-only GitHub service in the worker's `MCP_SERVERS` registry.
   SWF Testbed carries both Rucio catalogs; no Rucio credential or proxy is
   installed on the corun worker.
6. **A token** for the epicprod service account.

Everything else epicprod does itself over the existing surface: creates
its assessment section, template, and `campaign_assessment` definition
through 1–3; submits nightly runs (prompts + jobs, the two-POST contract
already deployed); receives completion through the existing subscription →
swf-monitor callback → Mattermost relay, unchanged; reads result pages,
run records, and prompt history over REST. Model settings epicprod sets in
the definition it creates: Codex Sol (`gpt-5.6-sol`) at `xhigh` reasoning
effort; a 900-second (15-minute) worker timeout. The system prompt requires
the assessor to calibrate its work and submit a complete report within ten
minutes, leaving five minutes only as termination margin (operator directive
2026-07-13).

### Artifact schema (v3, `schema_version: 3`)

The operative schema is `spec.validate_artifact` in
`swf_epicprod/assessment/spec.py`; the shape:

```json
{
  "schema_version": 3,
  "verdict": "ok | attention | alarm",
  "axes": {
    "arrivals":       {"status": "ok|attention|alarm", "note": "..."},
    "processing":     {"status": "...", "note": "..."},
    "failures":       {"status": "...", "note": "..."},
    "dispositions":   {"status": "...", "note": "..."},
    "infrastructure": {"status": "...", "note": "..."}
  },
  "assessment": ["<concise conclusion or operational implication>"],
  "activity_interpretation": ["<weekly relationship or trend>"],
  "software_findings": [
    {"finding": "...", "evidence": ["<link or tool reference>"],
     "significance": "<production effect>"}
  ],
  "top_issues": [
    {"title": "...", "severity": "attention|alarm",
     "evidence": ["<metric/action refs>"], "action": "<what a human should do>",
     "owner": "<who acts>"}
  ],
  "dismissed": [
    {"signal": "<what looked anomalous>", "reason": "<why it is not an issue>"}
  ],
  "outlook": ["<evidence-grounded expectation or decision>"],
  "narration": "2-4 self-contained sentences",
  "cites": {"narrative": "campaign_26.06.0", "narrative_version": 8,
            "evidence_computed_at": "<copy bundle.rollup.generated_at exactly>",
            "bundle_id": "<hidden corun Page group id>"},
  "generation": {
    "consulted": [{"source": "<tool or document>", "contribution": "..."}],
    "problems": ["<tool errors, gaps, workarounds>"],
    "unavailable": ["<sources or members that could not be obtained>"]
  }
}
```

The verdict and per-axis status vocabulary is exactly `ok | attention |
alarm`. The model emits this JSON and nothing else. Production code builds
the human report: deterministic interval and current-state tables from the
bundle, followed by the bounded judgment fields above. `narration` remains
the single payload for thin delivery channels. Before registration the
harness matches `bundle_id`, `evidence_computed_at`, and the narrative name
and version to the submitted bundle.

### Prompt templates

The operative templates are `DAILY_TEMPLATE` and `WEEKLY_TEMPLATE` in
`swf_epicprod/assessment/spec.py` — that module is the single source;
`assessment.bootstrap` pushes each as a versioned corun SystemPrompt
referenced by the kind's JobDefinition. This document does not carry
copies. Their stable professional contract is:

- Identity, audience, purpose, source semantics, tool capabilities, and
  investigation discipline are explicit. Editorial judgment belongs to the
  expert model; templates do not accumulate symptom-level presentation rules.
- The daily's subject is operational activity and change in the interval. The
  weekly is standalone and re-baselined against campaign intent and the
  production analytics record. Generated reports are not fed back as evidence.
- The readers are ePIC production and computing experts. Reports use their
  working vocabulary, identify and link concrete objects, and present time in
  ET.
- The model interprets and investigates; it does not calculate or restate the
  deterministic tables. The FLOOR is the minimum verdict, raise-only.
- Output is exactly one fenced JSON artifact. The harness renders the
  reader-oriented Markdown, including `###` sections, the bundle link, and the
  mandatory final `### Generation report`.

## Sequencing

During tuning, scheduled daily and weekly crons remain disabled. Manual runs
use the normal assessment path and register in the official AI assessment
series. Schema v3 separates deterministic facts from model judgment.
Scheduling is restored only after the corresponding report form
passes human review.

1. **Production side, first pass** — analytics members, rollup + floor, MCP
   tool + REST, `campaign` subject, trigger script. Two deploy cycles
   (swf-epicprod direct-to-main + a small swf-monitor change on
   baseline-v39).
2. **corun-ai side, parallel** — the REST completion (definitions,
   system-prompts, sections, run-outcome retrieval), the swf-testbed MCP
   registration, and a token. No harness work on the corun side.
2b. **Production side, harness** — the front end (basis assembly +
   two-POST submission) and the enforcement handler on the completion
   callback; epicprod bootstraps its section, template, and definition
   over the completed REST.
3. **End-to-end dry run** — manual trigger against the producing campaign;
   inspect the artifact, tune the floor thresholds and template.
4. **Scheduling gated on acceptance** — both cron lines were disabled
   2026-07-12 after the first outputs failed review. Restore daily only after
   an accepted daily report; restore weekly only after the daily form is
   stable and a weekly report is accepted.

The decision points held by the operator: floor thresholds and template
wording after the dry run (step 3), and the go for the crons (step 4).
Model/effort is Codex Sol (`gpt-5.6-sol`) at `xhigh` throughout tuning and
production.
