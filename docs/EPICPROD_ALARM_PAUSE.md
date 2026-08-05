# Alarm-Driven Task Pause

Plan for automatic pausing of PanDA tasks on catastrophic failure,
requested by S. Rahman (2026-08-05) after storage failures at Taiwan and
JLab drove task failure rates to 100% and wasted resources until an
operator intervened. The alarm system owns both the detection and the
response policy; execution reuses the verified PanDA task-operation
pipeline delivered for the manual pause/resume controls. Resume is
always manual.

This is a plan/design doc, peer to
[EPICPROD_OPS_AGENT.md](EPICPROD_OPS_AGENT.md) and
[EPICPROD_OPS.md](EPICPROD_OPS.md). The alarm engine itself is
documented in
[swf-monitor docs/alarms.md](https://github.com/BNLNPPS/swf-monitor/blob/main/docs/alarms.md).

## Architecture

The alarm system owns detection and response policy; the delivered
task-operation pipeline owns execution.

```text
alarm engine (5-min cron, standalone)
  detect: per-task failure rate over a moving window
  policy: alarm threshold → email; pause threshold → action
      │
      ▼  POST /api/panda/task-operations/alarm/   (DRF token)
swf-monitor service layer
  queue_task_operations(source='alarm')
  eligibility recheck: pause applies only to running tasks
  durable PandaTaskOperation record, pending-operation dedup
      │
      ▼  one paced prod-ops batch
epicprod-ops-agent (credential holder)
  Client.pauseTask per task, 1 s apart
  PanDA state verification on the shared deadline
  Capcom notice buffered, action stream recorded
```

Every safeguard of the manual path applies unchanged: fresh PanDA status
gating at queue time, one pending operation per task, paced scalar
commands, verified outcomes, durable per-task records, Capcom notices
through the polled buffer, and action-stream logging.

## Detection: windowed failure rate

The existing `panda_failure_rate_*` alarms compute a per-task
final-failure rate from JEDI's cumulative file-level accounting. A
cumulative rate answers "is this task unhealthy overall"; it lags the
catastrophic case, where a task that ran well for days fails 100% of its
jobs from one hour to the next. The pause trigger needs the rate over a
moving window.

Add a shared helper `windowed_task_failure_rate` in
`swf_alarms/common/`, yielding one `Detection` per task whose
job-terminal outcomes inside the trailing window exceed the configured
rate. It reads job-level terminal counts (finished vs. failed job
records with an end time inside the window) per running task, through
the same monitor REST surface the cumulative helper uses. Retry
inflation is acceptable here and inherent to the window: during a
storage outage every attempt fails, which is exactly the signal, and
the sample-size floor keeps retry noise from tripping a healthy task.

## Response policy: params on the alarm

Operators define both thresholds on the alarm config entry, in
`data.params` beside the existing detection params:

| Param | Type | Meaning |
|---|---|---|
| `window_hours` | float | moving window for the rate (Rahman's ask: 1–2 h) |
| `threshold` | float | alarm threshold: detection + email, as today |
| `pause_enabled` | bool, default false | master switch for the action |
| `pause_threshold` | float | rate at or above which the task is paused; must be ≥ `threshold` |
| `pause_min_terminal_jobs` | int | sample-size floor for the action, stricter than the detection floor |
| `pause_min_failed` | int | minimum failed jobs in the window for the action |

An alarm with `pause_enabled` absent or false behaves exactly as today:
detection, event rows, email. The action layer is additive; no existing
alarm changes behavior until an operator configures it.

## Trip lifecycle and idempotency

The alarm event row (stable `dedupe_key` `task:<jeditaskid>`) is the
trip record. On each engine tick, for each detection at or above the
pause policy:

1. If the active event for this task already carries a trip
   (`data.pause_operation_id`), do nothing — one pause per event
   episode.
2. Otherwise POST the single-task pause to the alarm operations
   endpoint and store the returned operation id and timestamp in the
   event data.

The condition is recomputed by the same tick that acts on it — the
detection and the action share one evaluation — and the service layer
rechecks live PanDA status at queue time, so a task that completed or
was already paused between reads is refused rather than acted on. The
pending-operation unique constraint refuses a duplicate while a manual
operation is in flight.

After a manual resume, the still-active event does not re-trip. If the
cleared condition later returns — the event clears when detection
stops, and a fresh event forms on a new detection — the new episode may
pause the task again. Resuming a task whose underlying failure persists
is therefore corrected automatically on the next window, which is the
intended circuit-breaker behavior. Automatic resume is excluded by
design.

## Execution path: the alarm operations endpoint

The manual endpoints gate on `is_tunnel_request()`, which is true for
any localhost request — including the alarm engine's. Alarm-sourced
operations therefore get their own endpoint:

- `POST /api/panda/task-operations/alarm/` — DRF token authentication
  only (the same machine-auth contract as the agent's lifecycle
  callback endpoint), no tunnel gate, no session path.
- Body: `{jedi_task_ids, operation: 'pause', alarm, evidence}` where
  `alarm` is the alarm entry id and `evidence` carries the window
  counts and rate that justified the trip.
- Forces `source='alarm'` and `requested_by=<alarm entry id>` into
  `queue_task_operations`; resume is rejected at this endpoint.
- The engine reads the token from its `config.toml`
  (`/opt/swf-monitor/config/alarms/config.toml`, hand-managed), where
  its database and email credentials already live.

## Visibility

- The alarm email for a tripped detection states the action taken and
  links the task page and the operation record.
- The Capcom notice flows from the agent through the polled notice
  buffer under `swf-panda-operations`, as for manual operations; the
  title carries the alarm source.
- The `swf-panda` Capcom state tile already counts paused tasks and
  turns yellow when any exist.
- The task detail and list pages already render paused state, the
  operation history, and the Resume control; the operation record's
  `source='alarm'` distinguishes automatic pauses there and in the
  action stream.

## Safety rails

- `pause_enabled` defaults false per alarm; the engine's `--dry-run`
  suppresses the action along with email.
- The engine acts only on same-tick detections, never on stored events
  from earlier ticks.
- The service layer's eligibility recheck and pending-operation
  constraint hold for alarm-sourced requests exactly as for manual
  ones.
- Per-request task cap (`MAX_BULK_TASKS`) applies; a storage failure
  tripping many tasks pauses them across successive 5-minute ticks
  rather than in one unbounded burst.
- Resume is manual only, from the task pages.

## Delivery steps

1. `windowed_task_failure_rate` helper plus the pause-policy params and
   their editor help-panel schema; a `panda_failure_windowed` alarm
   module delegating to it.
2. The token-authenticated alarm operations endpoint over
   `queue_task_operations`, with `source='alarm'` carried through the
   records, notices, and action stream.
3. The engine response step: policy evaluation, trip record in event
   data, POST, outcome logging; token in the engine config.
4. Alarm config entries authored by operations with production
   thresholds; initial deployment with `pause_enabled=false` observed
   against live detections before the switch is turned on.

Each step is independently deployable and testable; step 4's
observation period is the acceptance gate for enabling the action.
