# PanDA Ancillary Systems Audit

An audit of the systems surrounding the PanDA core — the VO-plugin tiers
inside the PanDA server and JEDI, the classic-server dataservice and
daemons, and the ecosystem components ATLAS operates around PanDA — read
against what the epic VO actually receives. ATLAS's implementations are
the guide: each gap between the ATLAS tier and the generic tier is a
candidate for an epicprod-side equivalent, a configuration change, or a
deliberate non-adoption. The audit was performed 2026-08-12 against the
nightly-synced source clones (`panda-server` including `pandajedi/`,
`panda-client`, `panda-docs`, `harvester`, `iDDS`, `pilot2`); file:line
citations refer to those trees.

Two findings from the same day's work motivated it: JEDI reopens a task's
output datasets at job generation for ATLAS but not for the generic VOs
(fixed epicprod-side as reopen-before-retry), and the PanDA server purges
idle sandbox tarballs with no generic keepalive (fixed epicprod-side as
the nightly sandbox keepalive). Both are instances of the pattern this
audit inventories.

## How plugin selection works

Two independent dispatch mechanisms exist.

**JEDI** (`pandajedi/`): every agent resolves its implementation through
`FactoryBase` (`jedicore/FactoryBase.py:106-139`) from a per-section
`modConfig` string in `panda_jedi.cfg` with format
`vo:sourceLabel:module:Class[:subType]`. Lookup is exact-match on the vo
string, then the literal key `any`, then `None`. There is no built-in
default: a VO with no entry gets task-refinement failures ("task refiner
is undefined"), broken tasks at post-processing, or attribute errors,
depending on the axis. The shipped template wires all non-ATLAS axes to
the `Gen*` classes under the vo key `wlcg`
(`templates/panda_jedi.cfg.rpmnew.template:72-326`).

**Classic server** (`pandaserver/`): the adder, setupper, and closer
resolve through `panda_config.getPlugin`
(`config/panda_config.py:217-232`). Here the fallbacks are hard-coded:
with no VO entry, any VO gets `AdderAtlasPlugin`
(`dataservice/adder_gen.py:159-163`) and `SetupperAtlasPlugin`
(`dataservice/setupper.py:103-109`); the closer's VO plugin runs only for
`VO == "atlas"` (`dataservice/closer.py:121-125`), and its config key
(`closer_plugins`) is never parsed at all
(`panda_config.py:236-238`), so `Closer.perform_vo_actions()` is a no-op
for every non-ATLAS VO by construction.

**The single highest-value verification item**: the BNL production
`panda_jedi.cfg` and `panda_server.cfg` are not in any local clone, so
which classes the epic VO actually receives on each axis is inferred, not
observed. The inference for the JEDI DDM axis is strong: epic log
datasets demonstrably get closed in Rucio at task finalization (observed
`closed_at` metadata), the only JEDI-side close path is
`GenPostProcessor.doPostProcess` → `ddmIF.freezeDataset`
(`jedipprocess/GenPostProcessor.py:26`), and `GenDDMClient` does not
implement `freezeDataset` (it implements exactly one method,
`jediddm/GenDDMClient.py:16-17`) — so the live epic DDM interface is
almost certainly `AtlasDDMClient`, which is Rucio-generic despite its
name. That would mean `openDataset` and the full 44-method DDM surface
are already available to the epic JEDI; the reopen gap was in the no-op
`GenTaskSetupper`, not in DDM capability. Reading the live configs (or
confirming with the PanDA team) pins every severity judgment below.

## Findings by axis — JEDI

| Axis | ATLAS tier | Generic tier (epic) | epicprod exposure |
|---|---|---|---|
| DDM client | `AtlasDDMClient`, 44 methods (freeze/open/delete/rules/quota/staging) | `GenDDMClient`: one stub method answering "closed" | Low if live config wires AtlasDDMClient (see above); otherwise every DDM-touching path is broken or lying |
| Task setup | `AtlasTaskSetupper`: registers datasets/rules, reopens datasets + clears lifetime at job generation (`:313-315`) | `GenTaskSetupper.doSetup` returns success, does nothing | **Closed epicprod-side**: reopen-before-retry in the task-operation doer (JEDI_INTEGRATION.md) covers the retry case, with a lifetime refresh the ATLAS path lacks |
| Post-processing | Freeze, stray-file reconciliation vs the DB, transient-dataset deletion, duplicate-task pause, exhausted transition, per-class dataset lifetimes (14d/40d/30d), completion e-mail | `GenPostProcessor`: freeze plus base bookkeeping only; no lifetime management, no reconciliation, no exhausted handling, no notification | Medium; see recommendations |
| Task refinement | ES auto-conversion, dataset-registration flags, input-consistency checks, attempt-number filename suffixes | `GenTaskRefiner`: sane defaults (`cloudAsVO`, `messageDriven`, RAM default), none of the above | Low for the current EVGEN path (`noInput`+`noOutput` bypasses most of it) |
| Job brokerage | ~45 selection stages (data locality, network, IO intensity, quotas, release matching, failure-rate avoidance) | `GenJobBroker`: 9 stages (status, disk, walltime, cores, memory, nPilot, availability, share weight) | Low while tasks pin `site=`; grows directly with multi-site brokered production |
| Task brokerage | `AtlasProdTaskBroker` assigns nucleus/cloud | No implementation exists for any other VO | Apparently none today (tasks carry explicit sites); verify how epic tasks pass the assigning state |
| Throttling | Thin ATLAS classes over a 265-line base engine (share-aware queue limits and caps) | `GenJobThrottler`: unthrottled if the workqueue has no share value, otherwise **unconditionally throttled** (`GenJobThrottler.py:22-26`) | Latent trap: assigning a global-share value to an epic workqueue would silently stop job generation for it |
| Watchdogs | `AtlasProdWatchDog` (failure-rate auto-pause, priority boost near completion, reassign with Rucio rule moves), `AtlasAnalWatchDog` (user quotas, priority massage), plus subtype dogs (QueueFiller, TaskWithholder, DataLocalityUpdater, DataCarousel) | `GenWatchDog`: inherits the base recovery loop (`TypicalWatchDogBase.pre_action:15-110` — pending-task reactivation, stuck-contents restart, exhausted kick, goal-reached auto-finish) but `doAction` is empty | The base recovery loop is real and epic has it. The `doAction` tier maps to epicprod's own agent chores — the alarm-pause is the failure-rate auto-pause already rebuilt |

## Findings by axis — classic server

- **Adder/setupper** — epic jobs are processed by the ATLAS plugins via
  fallback (subject to live-config verification). These are functional
  for epic: they register log files to BNL Rucio and create `_sub`
  datasets. One sharp edge: file scope is re-derived from the dataset
  name only for `VO == "atlas"`
  (`taskbuffer/db_proxy_mods/job_complex_module.py:3080`); epic files
  keep whatever scope JEDI assigned. Dormant, but a scope mismatch would
  surface as an adder registration failure.
- **Closed datasets are a fatal, non-retried job failure** —
  `UnsupportedOperation` is in `_FATAL_REGISTRATION_ERRORS`
  (`dataservice/ddm.py:46-58`) and nothing anywhere in the server reopens
  a dataset. This is the enforcement-level confirmation that
  reopen-before-retry was the only lever for the closed-log-DID failure.
- **datasetManager never closes or erases `group.*` datasets in Rucio**
  while still flipping their PanDA DB rows to completed/deleted
  (`daemons/scripts/datasetManager.py:194,348,1018`). PanDA-DB and Rucio
  state diverge by construction, and epic `_sub` datasets accumulate
  open in BNL Rucio indefinitely. A live count is a one-query check and
  a candidate for a periodic epicprod cleanup chore.
- **Sandbox cache** — purge at mtime > 7 days
  (`daemons/scripts/copyArchive.py:1284-1301`); job generation is what
  refreshes an active task's tarball; `touch_cache_file`
  (`api/v1/file_server_api.py:404-434`) is the supported keepalive lever,
  addressed per server host. Closed epicprod-side by the nightly sandbox
  keepalive. The API's `delete_cache_file` is a documented dummy.
- **Smaller ATLAS hardcodes that quietly exclude epic**: server
  core-hours metrics require `vo='atlas'`
  (`daemons/scripts/metric_collector.py:92`); watcher heartbeat-timeout
  config is read with `vo="atlas"` so `vo='epic'` CONFIG rows are
  invisible (`copyArchive.py:281`); production working-group extraction
  matches ATLAS FQANs only (`api/v1/common.py:145-158`), though the
  plain production-role check accepts any `/<vo>/Role=production`.

## Ecosystem components

Judged for epicprod: already in use, adoptable by configuration or data,
needing integration work, or not applicable.

- **In use already**: Harvester (VO-generic plugin host — credential
  managers, preparators/stagers, pull and UPS modes), BigMon
  (multi-instance by design), CRIC-style queue configuration.
- **Adoptable as data, no code** — **the Job Retry Module**: server-side
  automatic retry actions (`no_retry`, `limit_retry`, memory/CPU
  increase) driven by per-error-code rules in the `RETRYERRORS` /
  `RETRYACTIONS` DB tables (`taskbuffer/retryModule.py`, doc
  `advanced/job_retry_module.rst`). VO-generic framework; the rule
  content is per-instance data. epicprod's accumulated error taxonomy
  (payload phases, DDM closures, sandbox failures) maps directly onto
  rules — the highest-leverage adoption in this list. Also **Global
  Shares** (VO-keyed data model) — with the caveat that the generic
  throttler's share handling must be understood first (see the trap
  above).
- **Needing integration work if ever wanted**: iDDS (message-based task
  chaining, HPO, fine-grained carousel; generic framework, ATLAS-named
  reference plugin), Data Carousel (generic engine, ATLAS-only scheduling
  watchdog; tape staging is not currently an epic concern), Event
  Service (generic core, pilot-side support exists only for `atlas`,
  `generic`, `rubin` users).
- **No reusable component exists**: the ATLAS production-system layer
  (ProdSys2/DEfT) above PanDA is not open and not in panda-docs; only its
  DB schema (`T_TASK`, which every VO's tasks already pass through)
  survives in the core. PCS/epicprod is the epic equivalent of that
  layer, built natively.

## What epicprod has already rebuilt

Several epicprod capabilities are independent reconstructions of ATLAS
ancillary functions, arrived at from operational need. The
correspondence indicates where the remaining ATLAS ancillaries predict
future epicprod needs.

| epicprod capability | ATLAS counterpart |
|---|---|
| Reopen-before-retry (task-operation doer) | `AtlasTaskSetupper.openDataset` at job generation |
| Alarm-pause on failure rate | `AtlasProdWatchDog.do_task_progress_based_actions` auto-pause |
| Sandbox keepalive (nightly chore) | none — ATLAS tasks stay alive through continuous job generation |
| Campaign assessments + capcom notices | task completion e-mail (`AtlasAnalPostProcessor.doFinalProcedure`) |
| PCS composition/catalog/submission | ProdSys2/DEfT |
| Payload-log fetch + diagnosis | BigMon log access + ATLAS ops tooling |

## Recommendations and status

Items 2-4 were executed against the live systems on 2026-08-12; their
outcomes are recorded in place.

1. **Verify the live BNL configs** — narrowed by live evidence; one ask
   remains. The server is `pandaserver01.sdcc.bnl.gov` (every
   `jedi_process_lock` holder), database `pandadb01.sdcc.bnl.gov`, and
   the configs are not readable from the epicprod host. Live evidence
   from the running system fixes the config's shape: JEDI watchdog
   processes hold per-VO locks for `epic` and `wlcg` separately
   (`jedi_process_lock` rows, component `TypicalWatchDogBase.cache_tokens`)
   — so `panda_jedi.cfg` keys the epic VO explicitly and the generic
   watchdog family, including the base recovery loop, is live for epic;
   and the `[ddm]` client implements `freezeDataset` (epic log datasets
   get closed at finalization), which `GenDDMClient` does not — so the
   live DDM client is `AtlasDDMClient` or equivalent. The remaining ask
   for the PanDA team is the exact class per axis: `modConfig` entries
   covering epic in `panda_jedi.cfg` sections `[ddm]`, `[tasksetup]`,
   `[postprocessor]`, `[watchdog]`, `[jobthrottle]`, `[taskrefine]`,
   `[jobbroker]`, and the `adder_plugins`/`setupper_plugins` lines of
   `panda_server.cfg`.
2. **`_sub` dataset leak — checked, none exists.** BNL Rucio holds zero
   `group.EIC.*_sub*` datasets. The ATLAS setupper skips Rucio
   registration of `_sub` blocks when source and destination DDM
   endpoints coincide (`setupper_atlas_plugin.py:590-596`), which is
   epic's configuration, so the datasetManager name-skip has nothing to
   leak. No cleanup chore is needed.
3. **Job Retry Module — greenfield, rule set drafted.** The live
   `RETRYERRORS` and `RETRYACTIONS` tables are empty (no VO has rules on
   this instance), so the module currently takes no automatic actions.
   Proposed initial epic rules, to be loaded by the PanDA operations
   team, passive (`Active=false`) first:
   - DDM error 200, diagnostic matching `is closed` → `no_retry`
     (job-level retries against a closed dataset are structurally
     futile; task-level handling is the reopen-before-retry doer).
   - Executor 5303, tarball download failure → `limit_retry`
     (bounded, since the sandbox keepalive prevents the standing cause;
     a residual occurrence is transient or terminal, not improved by
     unbounded retries).
4. **Throttler trap — checked, currently safe.** All six epic and wlcg
   workqueues carry `queue_share = NULL`, the value for which
   `GenJobThrottler` does not throttle. The caveat stands: assigning a
   share value to an epic workqueue without replacing the generic
   throttler silently stops job generation for that queue.
5. **Dataset lifetime management at task end** (the `doFinalProcedure`
   gap) — open, design proposed. Epic log datasets receive a 30-day
   registration-time lifetime and nothing manages it afterward: logs of
   a task under post-mortem can expire 30 days after task creation
   regardless of when the task ended. The natural implementation is a
   lifetime refresh folded into the nightly sandbox keepalive — the
   candidate-task set is identical, the dataset lookup by `task_id`
   metadata is already performed there, and the reopen-before-retry
   doer already refreshes lifetime on retry. Pending a policy decision
   on the retention target.
6. **Task-brokerage and multi-site posture** — no action now (sites are
   pinned), but any move toward brokered multi-site production makes the
   `GenJobBroker`-vs-ATLAS gap (data locality, failure-rate avoidance,
   IO-intensity limits) the active constraint. Revisit then.

Items 3 and 5 follow the standing policy of implementing epicprod-side
equivalents with credentials already held, rather than requesting PanDA
server changes; item 3's rule content necessarily loads into the PanDA
database and goes through the PanDA operations team.
