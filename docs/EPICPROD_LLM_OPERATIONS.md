# ePIC Production LLM Operations

This document describes how ePIC production uses large language model (LLM)
operations from swf-monitor. It records the swf-monitor-side architecture:
where production context is held, where LLM-backed work is executed, where
artifacts are stored, and how asynchronous results reach browser pages.

swf-monitor remains the production-facing application. It presents PanDA, PCS,
queue, job, and campaign pages; records production state; and supplies the
production context passed to other services. corun-ai provides LLM-backed
operations and their durable artifacts. The two applications interact through
corun-ai REST APIs and swf-monitor callback endpoints.

## Component Responsibilities

| Component | Responsibility |
|---|---|
| swf-monitor | Production pages, production state, PanDA/PCS context, MCP tools for production objects, and browser rendering. |
| corun-ai | LLM-backed operations, prompts and configuration for those operations, durable artifacts, comments, provenance, and curation surfaces. |
| wrangle-ai | corun-ai's rapid asynchronous worker substrate for bounded LLM operations such as replying on a comment thread or running an assessment probe. |
| `epicprod_ops_agent` | Privileged production actions on `pandaserver02`: PanDA submission, Rucio/xrootd log retrieval, and related credentialed production work. |
| SSE relay | Browser notification for completed asynchronous work through `/api/messages/stream/`. |
| swf-remote | External proxying of swf-monitor pages, REST endpoints, static assets, and SSE streams. It does not contain production or LLM operation logic. |

The division is by function. Production credentials stay with the production
ops agent. LLM execution credentials and LLM artifact state stay with corun-ai.
Browser pages and production object context stay with swf-monitor.

## Operation Classes

### AI Assessments

AI assessments are append-only LLM artifacts attached to production objects.
swf-monitor exposes the subject context through MCP and records only a pointer
on the production object. The persistent artifact is a corun-ai Page in section
`epicprod.assessment`.

The swf-monitor MCP tool `epic_register_ai_assessment` creates the corun-ai Page
and appends the Page group id to the subject JSON field under
`corun_page_group_ids`. Object pages and MCP detail tools use those ids to
retrieve and render the assessment. The assessment text is not embedded in
PanDA task, job, queue, or PCS task records.

corun-ai Page metadata for these artifacts includes:

```json
{
  "artifact_type": "ai_assessment",
  "source_system": "swf-monitor",
  "ui_visible": false
}
```

`ui_visible: false` means the artifact is hidden from corun-ai/codoc browse
navigation. It is not an access-control boundary; direct URLs and authenticated
REST access remain available.

### LLM Comment Replies

LLM comment replies are rapid asynchronous operations over a corun-ai comment
thread. The initiating page is normally in swf-monitor, while the document,
comments, prompt configuration, and generated assistant comment are in corun-ai.

Expected flow:

1. A swf-monitor page requests an LLM reply for a corun-ai Page/comment thread.
2. swf-monitor calls a corun-ai REST endpoint for the request.
3. corun-ai creates a durable wrangle work item and returns a work id immediately.
4. A corun-ai wrangle worker loads the page, full comment thread, latest comment,
   and section-level followup prompt.
5. The worker appends an immutable assistant comment to the corun-ai thread.
6. corun-ai posts a completion callback to swf-monitor.
7. swf-monitor emits an SSE event through the existing browser stream.
8. The browser matches the work id or Page group id and refreshes the comment
   thread.

The browser observes completion through swf-monitor's SSE relay, not through a
direct browser connection to corun-ai. The callback is the server-to-server bridge
between corun-ai completion and swf-monitor browser notification.

### Campaign Narrative

The campaign narrative is a human-authored stream of corun-ai-backed entries for a
production campaign. The first entry records the campaign's purpose, priorities,
major elements, and starting scope. Later entries record changes in objectives,
priorities, or interpretation as the campaign develops.

Each narrative entry is a distinct artifact. Versioning is used for corrections
to an entry, not for the narrative sequence. Comments on entries support human
review and LLM followup. The narrative stream is an input to daily and on-demand
production assessment because it defines what campaign progress should be
measured against.

### Daily Production Reports

Daily production reports are scheduled LLM-backed operations over production
state, recent activity, and curated context such as the campaign narrative.
They are heavier than a rapid comment-reply operation. Their primary execution
path should be a scheduled job or cron-driven tool that calls REST services and
writes a durable report artifact, rather than a browser button as the main
interface.

The report artifact can still be stored and curated through corun-ai, with
swf-monitor rendering or linking it from production pages. On-demand report
generation may use the same interfaces, but the daily report should not depend
on an open browser session.

The scheduled campaign assessment design — nightly and weekly assessments,
the campaign analytics library, the artifact schema, and the harness
lifecycle — is [EPICPROD_ASSESSMENTS.md](EPICPROD_ASSESSMENTS.md).

## Asynchronous Completion

swf-monitor has one browser notification mechanism for asynchronous completion:
the existing Server-Sent Events (SSE) relay at `/api/messages/stream/`.

The event producer depends on the operation:

| Operation source | Completion path |
|---|---|
| Production ops agent | Agent publishes an event to `/topic/epictopic`; swf-monitor relays it through SSE. |
| corun-ai LLM operation | corun-ai posts a completion callback to swf-monitor; swf-monitor relays it through SSE. |

Browser pages use the same client-side rule for both cases:

1. Open a short-lived `EventSource` for the relevant `msg_type`.
2. Match the entity or work id in JavaScript.
3. Close the stream on match, timeout, or page unload.
4. Use an immediate status check and bounded fallback poll where a missed SSE
   event would otherwise leave the page stale.

SSE is a notification path. The source of truth is the database or corun-ai REST
state that the page reloads after receiving the event.

## REST and Metadata Conventions

swf-monitor should interact with corun-ai through REST APIs. corun-ai sections are
identified by section slug, such as `epicprod.assessment` or
`epicprod.narrative`. Section names are chosen by epicprod conventions, while
corun-ai treats them as generic section names.

Section metadata should be available through REST and may include operation
configuration. For LLM comment followup, the section can carry a standard prompt
such as:

```json
{
  "llm_comment_followup_prompt": "Please respond on the comment thread, taking account of the full thread and particularly the latest comment."
}
```

Artifact metadata should identify the producing system, artifact type, subject
reference, and visibility. Human comments remain editable by the human author
through the browser editor. LLM-authored comments are immutable and should carry
metadata identifying the work item, model, and prompt/configuration provenance.

## Implementation Rules

- Production object records store pointers to corun-ai artifacts, not copied LLM
  content.
- corun-ai service-owned artifacts use `data.ui_visible=false` when they should be
  hidden from corun-ai/codoc browse navigation.
- Browser-triggered LLM operations return quickly with a queued/work id.
- corun-ai performs LLM work server-side and reports completion through a
  swf-monitor callback.
- swf-monitor converts relevant corun-ai callbacks into SSE events for browser
  pages.
- Privileged PanDA, Rucio, and xrootd actions remain routed through
  `epicprod_ops_agent`.
- Scheduled production reports may use corun-ai artifact storage and LLM services,
  but their primary execution should be scheduled and independent of an open
  browser page.
