# PanDA Ancillary Systems Audit

An audit of the systems surrounding the PanDA core — the VO-plugin tiers
inside the PanDA server and JEDI, the classic-server dataservice and
daemons, and the ecosystem components ATLAS operates around PanDA — read
against what the epic VO receives. ATLAS's implementations are
the guide: each gap between the ATLAS tier and the generic tier is a
candidate for an epicprod-side equivalent, a configuration change, or a
deliberate non-adoption. The audit was performed 2026-08-12 against the
nightly-synced source clones (`panda-server` including `pandajedi/`,
`panda-client`, `panda-docs`, `harvester`, `iDDS`, `pilot2`); file:line
citations refer to those trees.

Two production repairs motivated it: JEDI reopens a task's output
datasets at job generation for ATLAS but not for the generic VOs
(addressed by the epicprod reopen-before-retry), and the PanDA server
purges idle sandbox tarballs with no generic keepalive (addressed by the
epicprod nightly sandbox keepalive). Both are instances of the pattern
this audit inventories.

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

**The live registrations**: the BNL production `panda_jedi.cfg` and
`panda_server.cfg` are not in any local clone; the deployed
registrations for the epic VO were read on the server itself and are
tabulated under recommendation 1. The axis tables below reflect them.
Notably, the epic `[ddm]` entry is `AtlasDDMClient` — Rucio-generic
despite its name — so `openDataset` and the full DDM surface are
available to the epic JEDI; the reopen gap was in the task setupper,
not in DDM capability.

## Findings by axis — JEDI

| Axis | ATLAS tier | Generic tier (epic) | epicprod exposure |
|---|---|---|---|
| DDM client | `AtlasDDMClient`, 44 methods (freeze/open/delete/rules/quota/staging) | `GenDDMClient`: one stub method answering "closed"; epic is configured with `AtlasDDMClient` (recommendation 1) | Low — epic has the full DDM surface |
| Task setup | `AtlasTaskSetupper`: registers datasets/rules, reopens datasets + clears lifetime at job generation (`:313-315`) | epic runs `SimpleTaskSetupper` (live config): registers datasets/containers/locations with a config lifetime, but has no reopen and no lifetime clear; `GenTaskSetupper` (the template default) does nothing at all | **Closed epicprod-side**: reopen-before-retry in the task-operation doer (JEDI_INTEGRATION.md) covers the retry case, with a lifetime refresh the ATLAS path lacks |
| Post-processing | Freeze, stray-file reconciliation vs the DB, transient-dataset deletion, duplicate-task pause, exhausted transition, per-class dataset lifetimes (14d/40d/30d), completion e-mail | `GenPostProcessor`: freeze plus base bookkeeping only; no lifetime management, no reconciliation, no exhausted handling, no notification | Medium; see recommendations |
| Task refinement | ES auto-conversion, dataset-registration flags, input-consistency checks, attempt-number filename suffixes | `GenTaskRefiner`: workable defaults (`cloudAsVO`, `messageDriven`, RAM default), none of the above | Low for the current EVGEN path (`noInput`+`noOutput` bypasses most of it) |
| Job brokerage | ~45 selection stages (data locality, network, IO intensity, quotas, release matching, failure-rate avoidance) | `GenJobBroker`: 9 stages (status, disk, walltime, cores, memory, nPilot, availability, share weight) | Low while tasks pin `site=`; grows directly with multi-site brokered production |
| Task brokerage | `AtlasProdTaskBroker` assigns nucleus/cloud | epic runs `AtlasProdTaskBroker` too (live config, `epic:managed\|test`) — no generic implementation exists, so the ATLAS one is config-assigned | Working today; its ATLAS-shaped assumptions (nucleus model, CRIC data) become relevant only with multi-site brokered production |
| Throttling | Thin ATLAS classes over a 265-line base engine (share-aware queue limits and caps) | `GenJobThrottler`: unthrottled if the workqueue has no share value, otherwise **unconditionally throttled** (`GenJobThrottler.py:22-26`) | Assigning a global-share value to an epic workqueue would silently stop its job generation (recommendation 4) |
| Watchdogs | `AtlasProdWatchDog` (failure-rate auto-pause, priority boost near completion, reassign with Rucio rule moves), `AtlasAnalWatchDog` (user quotas, priority massage), plus subtype watchdogs (QueueFiller, TaskWithholder, DataLocalityUpdater, DataCarousel) | `GenWatchDog`: inherits the base recovery loop (`TypicalWatchDogBase.pre_action:15-110` — pending-task reactivation, stuck-contents restart, exhausted kick, goal-reached auto-finish) but `doAction` is empty | epic inherits the base recovery loop. The `doAction` tier corresponds to epicprod's own agent chores; the epicprod alarm-pause matches the ATLAS failure-rate auto-pause |

## Findings by axis — classic server

- **Adder/setupper** — epic runs the explicit generic pair (live
  config): `AdderSimplePlugin` registers output/log files into the raw
  destination dataset in Rucio (no `_sub` propagation, no
  subscriptions, no dataset locations, and a hard-coded `default`
  output scope, `adder_simple_plugin.py:74`), and `SetupperDummyPlugin`
  creates no dispatch or destination datasets at all — dataset
  registration happens JEDI-side in `SimpleTaskSetupper` instead. One
  caveat: file scope is re-derived from the dataset name only for
  `VO == "atlas"` (`taskbuffer/db_proxy_mods/job_complex_module.py:3080`);
  epic files keep whatever scope JEDI assigned. Dormant, but a scope
  mismatch would surface as an adder registration failure.
- **Closed datasets are a fatal, non-retried job failure** —
  `UnsupportedOperation` is in `_FATAL_REGISTRATION_ERRORS`
  (`dataservice/ddm.py:46-58`) and nothing anywhere in the server reopens
  a dataset. This confirms at the enforcement level that reopening
  datasets before retry is the only remedy for the closed-log-DID
  failure.
- **datasetManager never closes or erases `group.*` datasets in Rucio**
  while still setting their PanDA DB rows to completed/deleted
  (`daemons/scripts/datasetManager.py:194,348,1018`), so PanDA-DB and
  Rucio state can diverge for any `group.*` dataset the daemon would
  otherwise manage. For epic this has no effect in practice: the dummy
  classic setupper creates no `_sub` or dispatch datasets at all
  (recommendation 2).
- **Sandbox cache** — purge at mtime > 7 days
  (`daemons/scripts/copyArchive.py:1284-1301`); job generation is what
  refreshes an active task's tarball; `touch_cache_file`
  (`api/v1/file_server_api.py:404-434`) is the supported counter-measure,
  addressed per server host. Closed epicprod-side by the nightly sandbox
  keepalive. The API's `delete_cache_file` is a documented dummy.
- **ATLAS hardcodes that exclude epic**: server
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
- **Adopted as data, no code — the Job Retry Module**: server-side
  automatic retry actions (`no_retry`, `limit_retry`, memory/CPU
  increase) driven by per-error-code rules in the `RETRYERRORS` /
  `RETRYACTIONS` DB tables (`taskbuffer/retryModule.py`, doc
  `advanced/job_retry_module.rst`). VO-generic framework; the rule
  content is per-instance data, and the first epic rules are loaded
  (recommendation 3). **Global Shares** (VO-keyed data model) is
  similarly adoptable as data, subject to the throttler share-handling
  constraint (see Throttling above).
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

Items 1-5 were executed against the live systems on 2026-08-12; their
outcomes are recorded in place.

1. **Verify the live BNL configs** — **closed 2026-08-12**: the live
   configs on `pandaserver01.sdcc.bnl.gov` were read directly. The epic
   VO's registrations, per axis:

   | Axis | Live class (epic) |
   |---|---|
   | `[ddm]` | `AtlasDDMClient` — the full Rucio surface, including `openDataset` |
   | `[tasksetup]` | `SimpleTaskSetupper` — real dataset/container/location registration (the source of the 30-day log lifetime); no reopen, no lifetime clear |
   | `[taskrefine]` | `GenTaskRefiner` |
   | `[jobbroker]` | `GenJobBroker` |
   | `[jobthrottle]` | `GenJobThrottler` |
   | `[postprocessor]` | `GenPostProcessor` |
   | `[watchdog]` | `GenWatchDog` |
   | `[taskbroker]` | `AtlasProdTaskBroker` for `epic:managed\|test` |
   | `[taskgen]` | `AtlasTaskGenerator` for `epic:managed\|test` |
   | `adder_plugins` | `AdderSimplePlugin` |
   | `setupper_plugins` | `SetupperDummyPlugin` |
   | `closer_plugins` | atlas-only; no epic entry (closer VO actions are a no-op) |

   Corrections this forces on the axis tables above: epic's JEDI task
   setup is `SimpleTaskSetupper`, not the no-op `GenTaskSetupper` — it
   registers datasets, but it equally lacks the reopen-at-job-generation
   block, so the reopen-before-retry conclusion is unchanged; epic does
   have a task broker (`AtlasProdTaskBroker`, config-assigned); and the
   classic-server tier is not the ATLAS fallback but the explicit
   generic pair — `AdderSimplePlugin` registers files into the raw
   destination dataset with no `_sub` machinery, and
   `SetupperDummyPlugin` creates no dispatch or `_sub` datasets at all,
   which — not the endpoint-coincidence skip — is why BNL Rucio holds
   zero epic `_sub` datasets (item 2). One configuration oddity
   observed: the `AsyncRequestWatchDog` entries sit in `[taskrefine]`'s
   `modConfig` rather than `[watchdog]`'s, and no watchdog process runs
   the `async_request` subtype, so that watchdog is registered but
   inert.
2. **`_sub` dataset leak — checked, none exists.** BNL Rucio holds zero
   `group.EIC.*_sub*` datasets. The live-config read (item 1) supplies
   the mechanism: epic's classic setupper is `SetupperDummyPlugin`,
   which creates no dispatch or `_sub` datasets at all, so the
   datasetManager name-skip has nothing to leak. No cleanup chore is
   needed.
3. **Job Retry Module — closed 2026-08-12: the first rules on this
   instance are loaded; both enforcing as of 2026-08-23.** The
   `RETRYERRORS`/`RETRYACTIONS` tables were empty for every VO;
   epicprod loaded the initial epic set directly (the tables are
   instance data and the epicprod database credential holds write
   access), passive at first and activated after observation:
   - `ddmErrorCode` 200, diagnostic matching `is closed` → `no_retry` —
     job-level retries against a closed dataset are structurally
     futile; task-level handling is the reopen-before-retry doer.
   - `exeErrorCode` 5303, diagnostic matching the sandbox tarball
     download failure → `limit_retry` with `maxAttempt=2` — one extra
     attempt covers transient failures; the keepalive prevents the
     standing cause.

   A rule enforces only when both its own `active` flag and its
   action's are `'Y'` (`taskbuffer/db_proxy_mods/misc_standalone_module.py`,
   `getRetrialRules`); the rule-level switch is managed with
   `swf-monitor/scripts/panda-retry-rules.py` (list, activate,
   deactivate; dry-run default). The retry module reloads rules on a
   one-hour cache; normal per-job attempt retries are untouched. The
   live rule set is displayed in the System page's PanDA Configuration
   section (`panda-retry-rules` collector).
4. **Throttler share handling — checked, currently safe.** All six epic and wlcg
   workqueues carry `queue_share = NULL`, the value for which
   `GenJobThrottler` does not throttle. The caveat stands: assigning a
   share value to an epic workqueue without replacing the generic
   throttler silently stops job generation for that queue.
5. **Dataset lifetime management at task end** (the `doFinalProcedure`
   gap) — **closed 2026-08-12**. Epic log datasets receive a 30-day
   registration-time lifetime and nothing managed it afterward: logs of
   a task under post-mortem could expire 30 days after task creation
   regardless of when the task ended. The lifetime refresh is folded
   into the nightly sandbox keepalive (identical candidate set, same
   `task_id` dataset lookup): datasets whose expiry falls inside the
   retention window are refreshed to the full window, datasets with no
   expiry are untouched, and the reopen-before-retry doer refreshes on
   retry. The first live run refreshed 94 of 114 checked datasets —
   most of the retryable-window population was inside 30 days of
   deletion.
6. **Task-brokerage and multi-site posture** — no action now (sites are
   pinned), but any move toward brokered multi-site production makes the
   `GenJobBroker`-vs-ATLAS gap (data locality, failure-rate avoidance,
   IO-intensity limits) the active constraint. Revisit then.

Items 3 and 5 follow the standing policy of implementing epicprod-side
equivalents with credentials already held, rather than requesting PanDA
server changes.
