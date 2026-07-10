# ePIC Production Operations Agent

`epicprod_ops_agent` is the always-on, credentialed executor for ePIC
production. It performs the privileged production actions — submit to PanDA,
stage logs from Rucio over xrootd, and the credentialed work to come — that the
public web tier structurally cannot. Building out epicprod functionality is, in
large part, adding capabilities to this agent: it is the intended instrument for
programmatic work against the privileged production services.

This is a design/planning doc, peer to [PCS.md](PCS.md),
[JEDI_INTEGRATION.md](JEDI_INTEGRATION.md),
[EPICPROD_TASK_CATALOG.md](EPICPROD_TASK_CATALOG.md), and
[PCS_DATASET_REQUEST_WORKFLOW.md](PCS_DATASET_REQUEST_WORKFLOW.md). Its
operations counterpart — how to run, restart, and monitor the agent, and the
concrete payload-log retrieval mechanics — is [EPICPROD_OPS.md](EPICPROD_OPS.md).
corun-ai-backed LLM operations are described separately in
[EPICPROD_LLM_OPERATIONS.md](EPICPROD_LLM_OPERATIONS.md); this document is about
credentialed production actions on `pandaserver02`.
The agent is built on the testbed's `swf_common_lib.base_agent.BaseAgent`, so it
inherits testbed agent management and monitor visibility like the other agents.

## Role — surfaces vs. executor

The system separates *presentation* and *API surface* from *privileged
execution*:

- **Web tier (Apache/Django)** presents pages and holds **no credential**. It
  reads world-readable caches and the database, and it drops messages on the
  bus. It never runs a privileged client.
- **REST and MCP** are thin peer API surfaces over the credential-free PCS
  service layer (`pcs.services`). REST serves the web UI and scripts; MCP is the
  LLM-facing API for bots and assistants. Both turn wire-format input into a
  service call — they are *surfaces*, not executors. **MCP is an LLM API, not an
  execution engine**: no PanDA, Rucio, or xrootd credential is ever wired into
  the MCP server or the web tier.
- **The agent** is the **credentialed executor**. It runs as the production-ops
  user (currently `wenauseic`), so it alone holds the keys — the Rucio x509
  proxy, the PanDA OIDC token, xrootd — and it is the single chokepoint through
  which every privileged action passes, whatever surface triggered it.

A trigger may arrive from the web tier, from REST, from an MCP tool driven by a
bot or assistant, or from cron. All of them resolve to one message on the
agent's queue, and the agent does the credentialed work. Human-driven decisions
drive deterministic execution; the agent is where that execution lives.

Concretely, this is what unblocks server-side submission. `JEDI_INTEGRATION.md`
records that submission from swf-monitor was blocked because the web service has
no PanDA identity. The agent removes the block without waiting for a robot
account: running as the operator, it reuses the operator's cached production
token and submits non-interactively. An OIDC service account remains the
long-term path (it would let the agent submit as a robot rather than as the
operator), but it is no longer a prerequisite for automated submission.

## Credential boundary

The agent runs under the production-ops user, not the web service, putting
ownership with production and keeping every credential out of the public-facing
tier:

| Credential | Used for | Held by |
|---|---|---|
| PanDA OIDC token (cached, `EIC.production`) | `prun` submission to JEDI | agent |
| Rucio x509 proxy (`longproxy-for-rucio`) | replica resolution, DID queries | agent |
| xrootd (`xrdcp`/`xrdfs`) | fetching log/output bytes | agent |

The web tier's only interaction with privileged results is to **read a
world-readable cache** the agent populates, or to **read the database record**
the agent updates. It holds nothing and runs nothing privileged. The
payload-log flow is the reference instance: the job page drops a
`fetch_payload_log` message and later serves the extracted log from the cache;
it never touches the proxy or xrootd.

**Where the agent writes — `$SWF_TMP_DIR`, never the deploy tree.** The agent
runs as the production-ops user (`wenauseic`, group `eic`); the web tier runs as
`apache`. Any artifact the agent produces for the web tier to read must live in
the agent-writable, world-readable shared cache `$SWF_TMP_DIR` (`/data/swf-tmp`)
— the tree the payload-log cache and the Rucio snapshots both use, with paths
derived from the `SWF_TMP_DIR` setting. It must **not** live under the deploy
tree (`/opt/swf-monitor/shared/...`), which is owned by `apache`: a doer that
writes there passes when the web tier writes the file and fails the instant the
agent takes over the write (`[Errno 13] Permission denied`). Like the
external-safe trigger, this is a boundary the internal/dev path hides until the
agent — not the web tier — performs the write.

## Capability model

The agent is **event-driven, not polled** — the same low-latency model the
testbed agents use, which matters as much for prod entities as for testbed
ones.

- **Queue and identity.** It subscribes to the anycast control queue
  `/queue/epicprod.ops`; a single consumer handles each request exactly once. It
  runs under a fixed `prodops` namespace (from `agents/prodops.toml`) so it is
  identifiable as the system singleton in the monitor and every caller addresses
  it explicitly (`namespace: prodops`). Foreign-namespace messages are filtered
  out.
- **Dispatch.** Each action is a `msg_type` routed to a `_handle_<msg_type>`
  method. **Growing the agent is adding a handler** — a new capability is a new
  `_handle_*`, registered in `KNOWN_TYPES`.
- **The doer pattern.** A handler is a thin event front end; the actual work is
  delegated to a standalone **doer script** (`scripts/cache-payload-log.py`,
  `scripts/submit-prod-task.py`) run as a subprocess under a timeout. Each doer
  is usable on its own, by cron, or by the agent — the agent does not embed the
  logic. **PCS stays the single source of truth**: the submit doer *fetches* the
  `prun` command from the PCS artifact endpoint rather than rebuilding it.
- **Robustness doctrine.** Every handler is **bounded** (a subprocess timeout)
  and **self-erroring** (it records its own failure where the triggering surface
  will see it — e.g. the `.error` marker in the payload-log cache). Handler
  exceptions are caught and logged; one sick capability never crashes the
  singleton. Control messages (`health_ping`, `shutdown`) do not flip the
  agent's processing state; work messages do, so the monitor shows the agent
  busy.
- **Outcome conventions, no polling.** A result lands where the trigger's
  surface already reads it: the cache (payload log) or the `ProdTask` record via
  `record-submission` (submit). Liveness replies on the bus (`health_ping` →
  `pong`). Nothing polls.

### Async execution — a BaseAgent capability

`BaseAgent` delivers messages on a single STOMP receiver thread, sequentially
(`ack='auto'`), so a handler that blocks stalls every later message — including
`health_ping`. A healthy `submit_task` returns in seconds, but the credentialed
work coming next is not all fast: the campaign-provenance sweep is a genuinely
long Rucio scan, and any privileged call can hang (this is distributed
computing). While the receiver thread is blocked, the cleaner-killer's liveness
ping (every ~2 min) goes unanswered and the watchdog restarts the unit —
killing the in-flight work it was meant to protect.

The fix is **threads, not asyncio**. The work is blocking subprocess/socket I/O
(`prun`, `xrdcp`, Rucio REST) and the stack is thread-based (stomp.py,
subprocess); an asyncio agent would buy nothing here and would force every agent
off the shared base. So the capability lives in `BaseAgent` itself — a
bounded worker pool, reusable by all agents — exposed as
`run_in_background(fn, *args, dedup_key=…, label=…)`. A handler enqueues its
doer and returns, freeing the receiver thread at once.

It is **opt-in**, which is what protects the other agents: one that never calls
`run_in_background` behaves exactly as before. The wrapper drives reentrant
PROCESSING state (PROCESSING while any background work is in flight), catches and
logs every exception (no silent worker death), and skips a call whose
`dedup_key` is already running (the duplicate-work race that concurrency
introduces). A send lock makes worker-thread bus sends safe; shutdown drains the
pool.

In this agent, `fetch_payload_log`, `submit_task`, and `rucio_snapshot_update`
enqueue via `run_in_background`; `health_ping` and `shutdown` stay inline. Because
`BaseAgent` lives in `swf-common-lib` and ships to every agent through the venv
chain, the other agents continue to heartbeat unchanged. The shared API is
documented in the `swf-common-lib` README.

## Building a new capability — the pattern

The testbed is becoming a live, automated, responsive production system, and this
agent is the standard instrument for it. A new credentialed, slow, or hang-prone
operation is not a new service — it is the same recipe, and the trigger comes
first because it is the step most often gotten wrong:

1. **Choose an external-safe trigger.** Most collaborators reach the agent
   through the swf-remote face (`epic-devcloud.org`), where the proxy carries no
   session or CSRF and cannot relay a redirect (3xx → 502). Only two trigger
   shapes survive that hop:
   - a **GET** page-view that drops the message as a side-effect and returns a
     body (200/202), never a redirect — `fetch_payload_log` via the job page; or
   - a **POST to `/pcs/api/`**, authenticated by `X-Remote-User`
     (`TunnelAuthentication`, csrf-exempt) and returning **JSON**, never a
     redirect — `submit_task` via the REST `submit` action.

   A page-view POST that relies on session+CSRF or ends in `redirect()` passes on
   the internal face and **fails through the proxy** — do not use it. See
   [EXTERNAL_ACCESS.md](https://github.com/BNLNPPS/swf-monitor/blob/main/docs/EXTERNAL_ACCESS.md) → *Write actions and triggers*, and
   verify on `epic-devcloud.org`, not the internal face, which hides the
   constraint.
2. **Handler + doer.** Add `_handle_<msg_type>` (validate, then enqueue) and a
   standalone `_do_<msg_type>` / `scripts/<doer>.py` that does the privileged
   work; register the type in `KNOWN_TYPES`. Any output the web tier will read
   goes under `$SWF_TMP_DIR`, not the deploy tree — see *Credential boundary*.
3. **Run it in the background.** Long work goes through `run_in_background`
   (bounded pool, dedup, reentrant PROCESSING) so the receiver thread never
   blocks — see *Async execution* above.
4. **Emit a completion event.** On success, publish a small event to
   `/topic/epictopic`; the existing SSE relay broadcasts it.
5. **Push it to the browser.** The triggering page holds an `EventSource` and
   updates the moment the event arrives — no polling, no manual refresh. See
   [SSE_PUSH.md](https://github.com/BNLNPPS/swf-monitor/blob/main/docs/SSE_PUSH.md).

The result is a button that fires a privileged action server-side under the
agent's credentials and reports back live — internally, and (through the
swf-remote streaming proxy) to remote collaborators. `fetch_payload_log` (GET
side-effect) and `submit_task` (`/pcs/api/` POST) are the worked examples for the
two trigger shapes; the campaign-provenance sweep is the next. Reach for this
pattern before building a poller, a blocking handler, or anything that places a
credential in the web tier.

## Current capabilities

Verified against `agents/epicprod_ops_agent.py` and its doers, 2026-06-02:

| `msg_type` | Doer | Credential | Outcome | Timeout |
|---|---|---|---|---|
| `fetch_payload_log` | `cache-payload-log.py` | Rucio proxy + xrootd | extracted log members in `$SWF_TMP_DIR/panda-logs/<jeditaskid>/<pandaid>/`, `.done` on success / `.error` on failure | 180s |
| `submit_task` | `submit-prod-task.py` | PanDA OIDC token (operator) | `jediTaskID` recorded on the `ProdTask` (`panda_task_id` + `status='submitted'`) via `record-submission` | 300s |
| `rucio_snapshot_update` | `rucio-snapshot-update.py` | JLab Rucio userpass (public `eicread`) | current+last snapshot refreshed, produced datasets matched onto each task's `overrides['outputs']`; `rucio_snapshot_ready` pushed (ok true/false) | 900s |
| `health_ping` | — | — | `pong` to `reply_to` | — |
| `shutdown` | — | — | deliberate stop; exits `EXIT_DELIBERATE=100` so systemd leaves it down | — |

**`fetch_payload_log`** resolves the log DID's replica (Rucio REST, x509,
account `panda`), `xrdcp`s the tarball, extracts the members into the cache, and
writes a `.done` sentinel. A miss publishes the message; a hit serves from
cache. On failure or timeout it writes an `.error` marker carrying the attempt
count and reason, which the web view surfaces and uses to bound retries.

**`submit_task`** runs the same `prun` an operator runs, non-interactively: it
GETs the `prun` command for the task from
`/pcs/api/prod-tasks/command/?name=<name>&fmt=panda`, runs it in a clean sandbox
under the panda-client environment with the cached token (never deleting
`$PANDA_CONFIG_ROOT/.token`, which would force an interactive device flow),
parses `jediTaskID=<N>`, and POSTs the outcome to
`/pcs/api/prod-tasks/record-submission/` as the task owner (`X-Remote-User`,
trusted on-host by the localhost tunnel). The task IS submitted even if the
final bookkeeping POST fails; that case is surfaced loudly with the task ID so
the operator can re-record.

The `submit_task` message is published by `services.prodtask_submit_request`,
behind the REST `submit` action (the two-pane compose view, and the task-detail
page's "Submit in Compose" link) — the **external-safe** trigger: a `/pcs/api/`
POST returning JSON. It is gated to `status='ready'` with no existing
`panda_task_id`, mirroring the `record-submission` gates so a submission whose
outcome would be refused is never fired. (The legacy `prod_task_submit_panda`
page-view submit — a page-POST+redirect that 502'd through the swf-remote proxy —
was retired.)

## Roadmap — capabilities as handlers

Each item below is, by design, a new handler + doer on this agent.

- **Async execution** (above) — implemented; the structural prerequisite for
  piling more long-running capabilities onto the singleton.
- **Campaign-provenance sweep** — the join Sakib's catalogue and the
  `eic/snippets` `check_campaign.py` / `check_storage.py` do by hand, run live
  and credentialed: for each requested EVGEN path
  `/volatile/eic/EPIC/EVGEN/<suffix>`, resolve the produced
  `epic:/RECO/<campaign>/<detector_config>/<suffix>` DID(s), their RSE replicas
  (BNL-XRD / EIC-XRD), and file counts, into a provenance cache the catalog
  renders. This supersedes the temporary hand-curated dataset catalogue and the
  monthly completion-status email.
- **Proactive completion notification** — auto-notify the operator the moment a
  fetch or submit finishes, removing the manual refresh; the corun-ai Mattermost
  callback is the working model. Browser-push design: [SSE_PUSH.md](https://github.com/BNLNPPS/swf-monitor/blob/main/docs/SSE_PUSH.md)
  (the agent emits `payload_log_ready` / `prodtask_submitted` over the SSE relay).
- **Credentialed MCP provider** — a future `pcs_prodtask_submit` MCP tool routes
  through the agent: the bot triggers, the agent (the credential holder)
  executes. The MCP server stays credential-free.
- **OIDC service account** — submit as a robot rather than reusing the
  operator's token; and **EVGEN-in-Rucio registration** once that workflow is
  defined.

## Operation

Running, restarting, monitoring, the systemd unit, the cleaner-killer cron
(reap duplicates / liveness / prune), the deliberate-stop back doors, and the
payload-log retrieval mechanics are in [EPICPROD_OPS.md](EPICPROD_OPS.md). This
doc does not duplicate them.

**Status (2026-07-05):** deployed and live on `pandaserver02`. Handlers:
`fetch_payload_log`, `submit_task`, `submit_evgen_task`,
`panda_task_operation`, `rucio_snapshot_update`, `evgen_rucio_update`,
`catalog_import`, `questionnaire_import`, `questionnaire_match_update`,
`campaign_progress_refresh`, `association_sweep` (with auto-intake of direct
group.EIC submissions), `catalog_sync` (the nightly composite chain, cron
02:15), `sync_epicprod_inventory`, `refresh_system_status`, `health_ping`,
`shutdown`. All work handlers run their doers on the `run_in_background`
worker pool and record structured action records — see
[ACTION_STREAM.md](https://github.com/BNLNPPS/swf-monitor/blob/main/docs/ACTION_STREAM.md).

## Action-stream logging

Every substantive handler records one structured record per outcome in the
epicprod action stream (`app_name='epicprod'`, instance `ops-agent` in AppLog),
via `_log_action()` — a REST-posting twin of
`monitor_app.epicprod_logging.log_epicprod_action`. Records carry the action
id, subject, requesting username where the message provides one, outcome
(`ok`/`error`/`timeout`/`unrecorded`), measured `duration_ms` around the doer
subprocess (every sweep reports its execution time), the declared `sublevel`
(importance: which humans the event reaches; changed by changing the
event), and the `live_default` recommendation for the live stream (effective
decision = the SysConfig `epicprod_live_policy` override, the runtime
attention knob on the live-policy page). Retrieval: `epicprod_list_actions`
MCP tool, the Logs page (`?app_name=epicprod`, or the Live stream toggle),
`swf_list_logs(app_name='epicprod')`.
