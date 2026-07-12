# Campaign Assessments V1 — Implementation Plan

This plan concretizes [EPICPROD_ASSESSMENTS.md](EPICPROD_ASSESSMENTS.md) into
a first working version of the nightly and weekly campaign reports: the hybrid
procedural / template / LLM design, delivered promptly and elaborated
thereafter. It is written 2026-07-12 as the plan of record for the v1 sprint.

Two workstreams, by ownership:

- **Production side** (this workspace: swf-epicprod, swf-monitor) — the
  analytics library, the campaign-status rollup and its MCP/REST surfaces,
  the mechanical verdict floor, the `campaign` assessment subject, the
  scheduled trigger, and surfacing.
- **corun-ai side** (ec2dev) — the assessment job definition, the harness
  around the LLM call, the artifact schema enforcement, and registration.
  This section is self-contained for handoff.

The design authority remains EPICPROD_ASSESSMENTS.md; where v1 cuts a corner,
the cut is recorded here.

## V1 scope

In: the nightly assessment end-to-end (trigger → evidence → LLM → validated
artifact → registration → live stream/Mattermost), the weekly assessment as
the same machinery with a seven-day window and larger prose budget, and an
analytics library whose v1 members wrap computations the system already
performs.

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
| `rucio_arrivals` | precomputed arrivals timeline (`load_rucio_timeline`) + `campaign.data['arrivals']` | daily files/bytes series by stage; last-arrival age; window totals |
| `panda_health` | bounded PanDA summaries already used by progress + the `nfinalfailed`/`computed_finalfailurerate` family (`monitor_app/panda/api.py`), error rollup per `panda_error_summary` | job-state counts, final-failure rate, top error codes with counts, active/finished task counts |
| `disposition_mix` | dataset `propagation` fields + history (PCS.md § Datasets) | counts by disposition; flips within window with comments |
| `action_stream_activity` | AppLog `app_name='epicprod'` | actions by id and outcome in window; chain-step durations; catalog_sync freshness (age of last successful chain) |
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
| `assessment_prior_count` | `7` | prior assessments supplied as trend context |

Credential warning/expiry reuses `CREDENTIAL_EXPIRY_WARN_DAYS`: warning →
`attention`, expired/missing → `alarm`.

Surfaces, peers over one service per the standing rule:

- MCP: `epicprod_campaign_status(campaign=None, window_days=1)` — campaign
  defaults to the producing campaign(s), else current. This is the tool the
  corun-ai worker calls for its evidence, and later the dashboard's source.
- REST: `GET /pcs/api/campaigns/status/?campaign=&window_days=` — same
  service function.

The rollup carries each member's `computed_at` so the harness can mark
staleness rather than silently accept old evidence.

### `campaign` assessment subject (swf-monitor, baseline-v39)

`epic_register_ai_assessment` gains subject type `campaign` (key: campaign
name as PCS records it, e.g. `26.06.0`; label and URL: the catalog view of
that campaign). Registration logs an epicprod action whose sublevel rises
when `data.verdict` is not `ok`, so an assessment calling for attention
reaches the live stream and the epicprod-live Mattermost channel with no new
machinery.

### Scheduled trigger (production host)

`scripts/assessment-trigger.py` in swf-epicprod — standalone, stdlib-only
REST client (no Django import), per the standing tooling preference. It:

1. reads the SysConfig gate through REST; exits quietly if disabled;
2. resolves the target campaign(s): producing first, else current;
3. POSTs to the corun-ai jobs API to create one `epicprod-assessment` job per
   campaign, parameters `{campaign, kind: nightly|weekly, window_days,
   requested_by}`;
4. records an `assessment_triggered` action-stream event per job (outcome
   `error` if the POST fails — a slot that never fills must be visible).

Cron (wenauseic), after the 02:15 catalog_sync chain has refreshed the state
being assessed:

```
45 3 * * *  nightly, every producing/current campaign
 0 6 * * 1  weekly, same targets, window_days=7
```

corun-ai URL and token live in `~/.env` / `production.env`.

### Surfacing (v1 minimum)

The assessment renders where campaign AI content already renders (the AI
pages); the live stream and Mattermost carry the narration through the
registration action and the existing corun-ai job-completion callback →
pandabot relay. Badges, filters, and the dashboard are the elaboration phase.

## corun-ai side (ec2dev) — self-contained handoff

corun-ai executes the assessment as a scheduled-triggered job; it holds the
LLM credentials, model choice, prompt template, and output schema, and stores
the artifact with provenance. Production state reaches it only through the
swf-monitor MCP tools; narratives and prior assessments are corun-ai's own
pages. Reference: EPICPROD_ASSESSMENTS.md (architecture, determinism rules,
harness lifecycle), EPICPROD_LLM_OPERATIONS.md (component responsibilities).

Deliverables:

1. **Job definition** `epicprod-assessment`: model settings decided
   2026-07-12 — Claude Fable for the 2026-07-12 bring-up runs; from
   2026-07-13, Codex 5.6 at `xhigh` reasoning effort (assessment quality
   matters more than latency). `mcp_tools` including the swf-testbed
   MCP server (the DISpatcher access path), timeout ~15 min, system prompt =
   the assessment template below, held as a versioned prompt group per the
   section-carried prompt convention.
2. **Intake**: the existing jobs REST API receives the trigger's POST; job
   parameters `{campaign, kind, window_days, requested_by}` reach the runner.
3. **Harness** (the deterministic wrapper around the model call):
   - Context: read the campaign narrative (`campaign_<campaign>`, current
     version) and the current general narrative (`campaign_general_*`
     latest); read the last N prior assessments of this campaign and kind
     (N from the rollup's `assessment_prior_count`).
   - Task 1 basis — assembled by the harness, deterministically. The
     must-look calls — `epicprod_campaign_status` (the rollup, carrying
     the verdict floor), PanDA activity and error summaries, task
     listings, the epicprod action stream, system status — are performed
     identically every run with per-call outcomes recorded, and bundled
     with the narratives and priors into one evidence bundle. Mandatory
     coverage is mechanism, not instruction: a failed must-look marks the
     run degraded mechanically. The must-look set is versioned harness
     configuration beside the template and schema. Each run's bundle is
     persisted: successive bundles form a time history of system state —
     the seed of a higher-frequency state timeline (recorded future
     direction, 2026-07-12: present monitoring is snapshot-oriented; a
     ~30 s system-state history is wanted).
   - Model drill-down: the summaries are surfacing instruments in their
     own right, not confirmations of the rollup; investigation is not
     gated on a rollup anomaly. Whatever any of them surfaces gets
     directed drill-down by the model (`panda_diagnose_jobs`,
     `panda_study_job`, `pcs_prodtask_get`, Rucio tools), plus any further
     summaries it wants mid-reasoning, bounded by the job timeout — every
     call lands in the verification transcript.
   - Task 2 — reason over the assembled picture and produce the report.
   - The model performs no arithmetic anywhere: every number it states
     must have arrived in a tool result during the run — the harness
     bundle or its own calls.
   - Generation report — always, with two authors: the harness's basis
     manifest (what was fetched, per-call outcomes, timings) and the
     model's own account — what it consulted and what each contributed,
     tool errors and gaps, anything unobtainable, workarounds taken — so
     a degraded run reads as degraded rather than smooth.
   - Validation: parse the structured block against the schema below; one
     bounded re-prompt on mismatch; second failure → quarantined artifact
     (marked malformed, raw output retained, excluded from later context)
     or a failure record. Every scheduled slot resolves to a visible outcome.
   - Number verification: every numeric value in the structured block must
     appear in the supplied evidence or in a tool result received during
     the run (the harness verifies against the run's tool transcript);
     violations are treated as schema failures.
   - Verdict floor: the rollup's `floor.verdict` is the minimum; the model
     may raise with justification, never lower.
   - One artifact per (campaign, kind, date); a rerun replaces its
     predecessor.
   - Registration: `epic_register_ai_assessment(subject_type='campaign',
     subject_key=<campaign>, assessment=<prose + narration>, ...)` with
     metadata `assessment_kind`, `origin: 'scheduled'`, `verdict`,
     `schema_version`, narrative citation (name + version), the structured
     block, and model/prompt provenance. Section: `epicprod.assessment`,
     `ui_visible: false`.
   - Completion: the existing job callback fires (pandabot already relays to
     Mattermost); no new notification machinery.

### Artifact schema (v1, `schema_version: 1`)

```json
{
  "verdict": "ok | attention | alarm",
  "axes": {
    "arrivals":       {"status": "ok|attention|alarm", "note": "..."},
    "processing":     {"status": "...", "note": "..."},
    "failures":       {"status": "...", "note": "..."},
    "dispositions":   {"status": "...", "note": "..."},
    "infrastructure": {"status": "...", "note": "..."}
  },
  "key_metrics": [
    {"name": "...", "value": "...", "delta": "...", "ref": "<object url or id>"}
  ],
  "top_issues": [
    {"title": "...", "severity": "attention|alarm",
     "evidence": ["<metric/action refs>"], "action": "<what a human should do>"}
  ],
  "dismissed": [
    {"signal": "<what looked anomalous>", "reason": "<why it is not an issue>"}
  ],
  "narration": "2-4 self-contained sentences",
  "cites": {"narrative": "campaign_26.06.0", "narrative_version": 5,
            "priors": ["<page group ids>"], "evidence_computed_at": "<iso8601>"},
  "generation": {
    "consulted": [{"source": "<tool or document>", "contribution": "..."}],
    "problems": ["<tool errors, gaps, workarounds>"],
    "unavailable": ["<sources or members that could not be obtained>"]
  }
}
```

The prose block is the page body; the structured block is `Page.data`; the
`narration` field is the single payload for every thin delivery channel.

### Nightly prompt template (v1)

```
You are the nightly production assessor for ePIC campaign {campaign},
assessment date {date}. You are given:

1. CAMPAIGN NARRATIVE {narrative_name} v{narrative_version} — what this
   campaign is for and what should be running.
2. GENERAL NARRATIVE — standing facts of how production operates.
3. PRIOR ASSESSMENTS — your last {n} nightly artifacts for this campaign.
4. THE PRODUCTION TOOLSET — the swf-testbed MCP tools (panda_*, pcs_*,
   epicprod_*, Rucio).

TASK 1 — build the comprehensive picture, from the campaign on down.
Your basis arrives assembled: the evidence bundle carries the campaign
status rollup (with the mechanical verdict FLOOR), PanDA activity and
error summaries, recent production actions, and system status, beside
the narratives and your prior assessments. Read every summary as a
surfacing instrument, not a confirmation of the rollup. Extend the
picture wherever anything surfaced warrants it: drill down to the task,
the site, the error, the action behind it, and call any further tools
you need. Directed digging within your time budget, in service of the
verdict and top issues.

TASK 2 — reason over the picture and report: correlation across signals
(an arrivals dip, one site's error spike, and a queue alarm may be one
event, not three), root causes your investigation established, deviation
from the narrative's stated intent, trend inflection against your prior
assessments, and the explicit call on what requires human action, if
anything. Signals you examined and set aside go in the dismissed list
with reasons — tomorrow's run inherits your explanations. Do not restate
chart or table contents. A quiet night is a valid result: say so briefly.

You interpret and investigate; you do not calculate. Every number you
state must have arrived in a tool result during this run; it is verified
against them. The FLOOR is your minimum verdict — raise it with
justification if warranted, never lower it.

ALWAYS close with the generation report: what you consulted and what each
contributed, tool errors or gaps you hit, anything you could not obtain,
and workarounds you took. Candour here is prized — a degraded run must
read as degraded, never as smooth.

Produce the artifact in the required JSON schema, then the prose block.
The narration field must stand alone: campaign, date, verdict, and the
one or two things that matter.
```

### Weekly prompt template (v1)

The nightly template with: "nightly production assessor" → "weekly
production assessor"; window seven days; priors = the week's nightlies plus
prior weeklies; and one added directive:

```
Measure the week against the campaign narrative's stated goals: what the
narrative says this campaign should accomplish, what progressed, what
stalled, and whether the campaign's trajectory this week supports its
intent. Trend interpretation is where your judgment carries the most value;
a larger prose budget is available and should be spent there.
```

## Sequencing

1. **Production side, first pass** — analytics members, rollup + floor, MCP
   tool + REST, `campaign` subject, trigger script. Two deploy cycles
   (swf-epicprod direct-to-main + a small swf-monitor change on
   baseline-v39).
2. **corun-ai side, parallel** — job definition, harness, schema validation,
   registration. Exercised first against the live
   `epicprod_campaign_status` tool from step 1.
3. **End-to-end dry run** — manual trigger against the producing campaign;
   inspect the artifact, tune the floor thresholds and template.
4. **Crons installed** — first scheduled nightly runs that night; the first
   weekly is run manually to seed the series, then rides its Monday cron.

The decision points held by the operator: floor thresholds and template
wording after the dry run (step 3), and the go for the crons (step 4).
Model/effort was decided 2026-07-12: Fable for bring-up, Codex 5.6 at
`xhigh` from 2026-07-13.
