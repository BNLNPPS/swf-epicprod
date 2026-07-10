# swf Architecture Map

This map is the plan of record for what lives where in the swf code
base: for each component, its current home, its destined home, and the
interface through which it is consumed. It implements the architecture
settled at the start of the v38 cycle (2026-07-10): the platform has
two homes — `swf-common-lib` for importable library code and
`swf-monitor` as the common monitor, web, and database service — and
the production domain moves to `swf-epicprod`. There is no separate
platform repository. Production is the first workflow domain to take a
peer application repository; the foreseen domains — distributed CI,
calibration, validation, analysis — follow the same pattern as they
mature: a peer application on the same platform.

Two rules govern the map:

- **Placement test.** Would `swf-testbed`, or a future swf application
  (distributed CI, calibration, validation, analysis, datataking), use
  the component unchanged? Yes: it is platform. If it speaks production
  vocabulary — campaigns, production tasks, dispositions, EVGEN — it is
  production domain.
- **Interface rule.** Components are consumed through their stated
  interface — web pages, REST, MCP, the message bus, or an
  `swf_common_lib` import — never by importing another application's
  source. A consumer bound to an interface survives relocation of the
  source.

Borderline components resolve by a mechanism/policy split: the engine
is platform, the production-specific registry or configuration is
domain. The adjudications section records the cases decided
individually.

`swf-testbed` (the streaming workflow testbed application) and
`swf-remote` (the external web face, separately deployed) are peer
applications with their own repositories and are not mapped here. The
testbed's monitoring entities live inside swf-monitor as its original
resident domain and stay there.

## Platform library — swf-common-lib (in place)

| Component | Contents | Interface |
|---|---|---|
| Agent base | `BaseAgent`, background worker pool, heartbeat/registration | import |
| Messaging | ActiveMQ/STOMP communication | import |
| Logging | REST log handler to the monitor | import |

## Platform services — swf-monitor (in place)

| Component | Contents | Interface |
|---|---|---|
| Web envelope | Django project, settings, deploy, Apache/ASGI, base templates and navigation | web |
| Authentication | CILogon/Auth0 integration, tunnel auth (`X-Remote-User`), middleware | web/REST |
| Logging service | `AppLog`, database log handler, Logs pages | REST/web |
| Action-stream machinery | sublevel and live axes on `AppLog`, live-policy registry mechanism, live stream view | REST/web |
| SSE relay | message-bus consumption, browser event stream (`/api/messages/stream/`); Redis-backed Channels layer for cross-process fanout | SSE |
| SysConfig | database system configuration, System-page editor, read-or-seed access | web/import* |
| Alarms engine | alarm configuration, engine, runner, event and report pages | web |
| MCP runtime | server, transport, tool registration, watchdog | MCP |
| State services | `PersistentState`, `UserPreference`, system status cache and pages | web/REST |
| PanDA processing service | task/job/queue/error/activity monitoring pages and queries, PanDA MCP tools — serves every PanDA-using domain: production, distributed CI, calibration | web/REST/MCP |
| AI integration service | corun-ai client, assessment registration, proposal ledger machinery, AI content retrieval — every domain reasons with AI through this one interface | web/REST/MCP |
| Testbed monitoring (resident domain) | agents, runs, STF files, TF slices, fast monitoring, workflow views, testbed bot | web/REST/MCP |

The platform services run on three backing stores, themselves platform
infrastructure: PostgreSQL (`swfdb`, the one system database), ActiveMQ
(the message bus), and Redis (the Channels layer for SSE fanout;
required in production per `SSE_RELAY.md`). Domain code reaches them
only through the platform's interfaces — the ORM within the envelope,
the bus through `swf_common_lib` messaging — never a direct Redis
client.

*Import is legitimate within the swf-monitor envelope, including from
installed swf-epicprod applications, which run inside it.

## Production domain — destined for swf-epicprod

| Component | Contents | Current location | Interface |
|---|---|---|---|
| Production documentation | `EPICPROD_*.md`, `PCS*.md`, `JEDI_INTEGRATION.md`, `COMMISSIONING_RELAXATIONS.md`, related design docs | `swf-monitor/docs/` | n/a |
| PCS | tags, datasets, campaigns, requests, tasks, configs; catalog, compose views, request composer; instancing, physics-configuration resolution, name tokens; `/pcs/api/` | `src/pcs/` | web/REST/MCP |
| Production AI content (thin) | production proposal types and decision surfaces, campaign assessment subjects and configuration | `src/ai/` | web/REST/MCP |
| PanDA production layer (thin) | production job/file inventory, campaign-task associations, production diagnosis, DISpatcher production assistant, corun-ai callback | `src/monitor_app/panda/`, views and models in `monitor_app` | web/REST/MCP |
| Production operations agent (instance) | `epicprod_ops_agent` and its doer scripts (submission, payload log, Rucio sweeps, catalog imports, cleaner-killer, enqueue) — the agent pattern itself is platform (`swf-common-lib`), with the testbed agents its precursor instances | `agents/`, `scripts/` | bus/REST |
| Production action definitions | production action ids, sublevels, live defaults — the registry infrastructure generalizes as platform, like the logging system it rides on | `monitor_app/epicprod_logging.py` | import (within envelope) |
| Production MCP tools | `pcs`, `epicprod_actions`, `ai_content`, `ai_proposals` tool modules | `src/monitor_app/mcp/` | MCP |
| Campaign assessments (new) | analytics library, rollup service, assessment harness glue | greenfield | REST/MCP |

Production Django applications ship from swf-epicprod as installable
packages listed in swf-monitor's `INSTALLED_APPS`; the monitor retains
thin adapters only (MCP tool registration, navigation entries).

## Adjudications

- **PanDA** is the hardest placement call, resolved by recognizing that
  PanDA processing is wider than production: distributed CI and
  calibration run on PanDA as well. The PanDA monitoring machinery — task, job,
  queue, error, and activity views, queries, and MCP tools — is
  therefore platform and stays in the monitor. The production-specific
  layer on top of it (inventory, campaign associations, production
  diagnosis) is domain, and the active direction is not migration but
  factoring: keep the production layer thin and separable over the
  shared machinery.
- **Action stream**: the machinery (log model, axes, live policy
  mechanism, stream view) is platform; the epicprod action registry
  and namespace helper are domain.
- **Alarms**: the engine is platform; production alarm configurations
  are data, owned by the domain.
- **SysConfig**: the mechanism is platform; production keys are data.
- **AI integration**: platform. Every domain — production, testbed,
  distributed CI, calibration — reasons with AI through the same
  corun-ai interface; AI is pervasive in the system, not a production
  feature. The mechanism (ledger, registration, retrieval, client) is
  platform; production proposal types and assessment configuration are
  the thin domain layer.
- **Operations agents**: the pattern is platform — `BaseAgent`, the
  worker pool, and the handler/doer structure live in `swf-common-lib`,
  and the testbed agents are the pattern's precursor instances. Each
  domain runs its own credentialed instance; `epicprod_ops_agent` and
  its doers are the production one.
- **Action registry**: the registry infrastructure (declaration of
  action ids, sublevels, live defaults) generalizes as platform, like
  the logging system it rides on; the production action definitions are
  the domain content.
- **MCP**: the runtime is platform; tool modules follow their domain;
  a registration shim remains in the monitor.
- **Legacy AI content and memory models** (`AIContent`, `AIMemory`):
  superseded by corun-ai artifacts; they retire in place and are not
  mapped for migration.

## Migration order

1. **Documentation first.** Each moved document leaves a permanent
   one-line stub at its old path linking to the new location;
   published links to `swf-monitor/docs/` never break. Doc indexes and
   the RTD operations-page link are updated in the same change.
2. **Greenfield next.** New production work is born here — first the
   campaign assessments arc ([EPICPROD_ASSESSMENTS.md] step 1) — and
   proves the packaging mechanics migrations will ride.
3. **Existing components case by case**, when the alternative is
   substantial new work landing in swf-monitor. A component jumps the
   queue when another party is about to consume it from its current
   location. There is no scheduled refactoring program. PCS is the
   natural first code migration: it is the one component that is
   entirely production domain, and it is well encapsulated.

[EPICPROD_ASSESSMENTS.md]: https://github.com/BNLNPPS/swf-monitor/blob/infra/baseline-v38/docs/EPICPROD_ASSESSMENTS.md
