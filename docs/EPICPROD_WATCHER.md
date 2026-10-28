# Production Watcher

The production watcher makes sure that every known open problem in ePIC
production gets an outcome. Every thirty minutes a mechanical tick reads
the production record and lists what is open. When something is new or
has changed, it wakes one dedicated LLM session. That session gives each
open item one of three outcomes: it acts, it puts the decision to the
operator with a recommendation, or it records what the item is waiting
on and until when. An item still without an outcome two ticks after it
appeared is escalated to epicprod-live at red level by the tick itself,
whether or not any model ran. The four-hourly live watch
([EPICPROD_ASSESSMENTS.md](EPICPROD_ASSESSMENTS.md#the-live-watch))
reviews what the watcher did.

## Why

The system already detects most of what goes wrong. The alarm engine,
the node guard, the room's report and the production notifier
([EPICPROD_OPS.md](EPICPROD_OPS.md#production-notices)) each raise their
conditions. Raising a condition does not close it. A notice delivered to
an interactive session waits on that session's attention, and a session
can answer with a question to an operator who is not there. On
2026-10-23 a failure cascade ran for hours that way, with the remedy
already proposed. The watcher closes the gap between a raised condition
and an action. The tick is deterministic and its escalation does not
depend on the model, so a stopped session or a missed message cannot
leave a known problem unattended.

## The tick

`scripts/prod-watch.py` runs from cron every thirty minutes under the
operating account. Like the notifier, it boots no Django: it reads the
record through the monitor's MCP tools over the loopback endpoint and
its http face. The notifier's readers move into a module beside the two
scripts, which both import. The tick reads the room's snapshot as stored
and never forces a rebuild.

| Source | Reader | Item key | The item clears when |
|---|---|---|---|
| The room's report, `report.items` (swf-monitor COMP_SNAPSHOT.md, Report and errors): the front's state, each amber or red health part (activity, canary, failures, pilot supply), every active alarm event (pings and the task final-failure-rate alarm among them), storage parts, sites not live, assessment arrival | `swf_comp_snapshot` | `report:` and the line's subject, the text before its first ": " | the line leaves the report |
| The room's live screen, `live.items`: live records at warning or above from the last twelve hours | `swf_comp_snapshot` | `live:` and the log record id | the record leaves the floor: recovered, superseded, or cleared with `epicprod_live_clear` |
| Paused production tasks, `paused.items` | `swf_comp_snapshot` | `paused:` and the task | the task is no longer paused |
| The notifier's triggers: `task.failing`, `queue.starved`, `platform.error`, `credential.expiry`, `arrivals.missing`, `nodeguard.trip` | the shared readers | the notifier's own key | the notifier's clearing condition |
| Promises: a check a session has committed to, recorded as a `watch_promise` action with what and by when | `epicprod_list_actions` | `promise:` and the record id | a `watch_outcome` names it |
| Queue drift: a copy of a queue's status that disagrees with the queue registry (QUEUE_REGISTRY.md) | the registry's drift check | `drift:` with the queue and the copy | the copy matches |
| The watcher session itself | TeamComms `list_sessions`, and the age of the session's last outcome | `watcher:session` | the session is online and answering |

A promise opens as an item at its due time if no outcome names it. The
operator's activities in TJAI carry verification steps as text; the tick
does not parse them. A check that needs watching is recorded as a
promise when it is made.

## Items and outcomes

The tick keeps its items in a state file,
`/data/wenauseic/swf-epicprod/prod-watch-state.json`. Each item holds its
key, source, the time it was first seen, the time its evidence last
changed, the evidence itself (the line, its level and its link), its
current outcome, and the time it was escalated. The action stream holds
the outcomes; the state file is the tick's working copy, rebuilt from
the sources and the outcome records if lost.

An outcome is a `watch_outcome` action naming the item key, written by
the watcher session through the monitor's log endpoint (`/api/logs/`,
the endpoint `scripts/live-clear.py` writes through), with one of three
kinds:

| Kind | Meaning | The item afterwards |
|---|---|---|
| `act` | The watcher took an action within its authority; the record names the action's own record | open until its source clears; an action that does not verify is a new item |
| `push` | The decision belongs to the operator: the record carries the recommendation and the answer wanted, and reaches the operator's Capcom feed by subscription | waits on the operator's answer; presented again once a day while open, or at once when its evidence changes |
| `wait` | The item depends on something under way: the record says what, why, and until when | quiet until that time or until its evidence changes, then open again |

The watcher's authority is that of the control tiers
([EPICPROD_OPS.md](EPICPROD_OPS.md#operational-control-tiers)). It acts
on what the unattended tier and the standing exceptions allow: a task in
a failure cascade is paused and reported at red level; a live item whose
question has an answer is cleared with its reason; an idempotent pass
that failed is enqueued again. It holds no credential of its own. A
pause is a request through the monitor's task-operation endpoint
(`/api/panda/task-operations/`), which the production operations agent
executes and verifies. Every action in the authenticated-operator tier
is pushed.

The watcher has no authority to exclude an OSG glidein site (Torre,
28 October 2026); a site failure is pushed with the site's finished and
failed jobs over the incident and the days before it. The node guard's
black-hole node exclusion is the only automated exclusion. Every wake
instructs the existing session to reread its current brief, so updated
authority takes effect without a session restart.

A study is never pushed (Torre, 26 October 2026, on a Poke that asked
him to approve a study of GREX registration failures: "I NEVER EVER EVER
WANT TO SEE THAT QUESTION"). Reading logs, studying jobs and finding a
cause need nobody's approval. The watcher studies the item itself or
sends the study as work to the session that owns it, and records a
`wait` on that session. Only a decision that follows from the finding,
and is the operator's, becomes a `push`. A study put to the operator is
the review's second case, a push of a decision that was the watcher's to
make.

Routine service recovery is also standing authority. The watcher weighs
the production cost of waiting against the bounded risk of a reversible
remedy and acts when the benefit is greater. It may move disposable caches,
preserve and verify closed logs before reclaiming their space, rerun an
existing credential-renewal recipe, and restart a failed service through
its documented controls after correcting the cause. Job/spool data,
science outputs and active logs are preserved. Recovery is verified through
space, service health and actual communication or new work, then recorded
as `act`. An approval request for a riskier deletion does not justify
leaving a shared outage unmitigated when a safer remedy is available.
Destructive or uncertain recovery, campaign decisions, new credential
identities, security-policy changes, resource expansion and outreach keep
their existing gates.

## The wake

A tick wakes the watcher session when it finds a new item, an item
whose evidence changed, a wait that has expired, or a promise that has
come due, or an open item the last wake did not list. A tick that finds
none of these sends nothing: answering items only shrinks the open set,
and that wakes no one. An item that clears and returns within two hours
with the same evidence is the same item, so its first sighting and its
outcome stand. The tick summary names each wake's reasons (new, changed,
reopened, unlisted). The wake is one
TJAI message to the watcher session, found by its name `swf-watch` among
the host's sessions; the session's inbox receiver delivers it as a turn.
The content lists every open item without an outcome, with its key,
source, evidence, link and age. TeamComms Notify LLM becomes the route
once a program connector is configured for the watcher (none is on this
host today).

The watcher session is a Claude Code session under the operating
account, on the subscription login, never metered access, in auto
permission mode. `scripts/prod-watch-session.sh`, run by the user unit
`scripts/systemd/prod-watch-session.service`, keeps it running in tmux
(session `swf-watch`), restarts it when it exits, and restarts it daily
so its context starts from its brief, EPICPROD_WATCHER_BRIEF.md. It
answers each wake with one outcome record per item through
`scripts/watch-record.py`.

When the watcher session is offline, the tick sends the digest to the
working sessions on the host through TJAI messaging, the notifier's
route, and says so at its head.

A headless run for each wake (`claude -p`, the doer of the wrangler
agent, swf-monitor WRANGLER_AGENT.md) carries no context from one wake
to the next, so a condition followed across ticks would be re-read from
nothing each time. It becomes the wake path when the wrangler agent is
built: a `watch_respond` worker per digest, with the same contract.

## Escalation

The tick escalates an item that has no outcome at the second tick after
it appeared, an hour at most. The escalation is one
`watch_escalated` action at outcome error, importance high, live by
default. It reaches epicprod-live at red level through the existing
subscription, the Capcom feed through a subscription of its own, and the
host's sessions through TJAI messaging. It names the item, its evidence,
its age, and whether the watcher session was online. The publication
policy (swf-monitor NOTICE_ROUTING.md) keeps an identical repetition
quiet; an escalated item is escalated again when its evidence changes.
Escalation runs in the tick, so it works with the watcher session
stopped, with TeamComms unreachable, and with no model available.

## Review

The live watch's evidence gains the watcher's records over its window:
the items opened and closed, each outcome, and each escalation. Its
judgment covers four further cases: an action that should not have been
taken, a push of a decision that was the watcher's to make, a wait whose
reason did not hold, and an escalation caused by a missing outcome. Its
findings are corrections to the watcher's brief.

## Cost

| Quantity | Expected |
|---|---|
| Ticks | 96 a day, no model; each reads the stored snapshot, one summarized action query and the notifier's readers, in under a minute |
| Wakes | 10 to 20 a day: the notifier found something in 400 of its 10,612 five-minute runs over the 37 days to 2026-10-23, about 11 a day |
| Records | about 100 quiet `watch_tick` records and a few dozen outcomes a day; the state file under 100 KB |
| Escalations | none while the watcher answers; each is one channel post |

## Built on, and new

Built on: the room's snapshot and its report, live screen and paused
pillar; the alarm engine's events and pings; the node guard; the
notifier's readers and its TJAI and TeamComms delivery; the action
stream, its log endpoint and notice routing; `epicprod_live_clear`; the
task-operation endpoint and the operations agent's verified pause; the
live watch harness.

New: the tick script and its state file; the actions `watch_tick`,
`watch_promise`, `watch_outcome` and `watch_escalated`, declared in
`ACTION_DEFAULTS`; a small script that writes promises and outcomes
through the log endpoint; the Capcom subscriptions on push outcomes and
escalations; the watcher session's unit and brief; the live watch's
added evidence.

## Build order

1. The tick, the outcome and promise records with their writer script,
   the watcher session and the wake, together, so known items get
   outcomes from the first day. Escalation runs in shadow for that day:
   each escalation it would make is recorded, not sent, which measures
   the volume before it posts.
2. Escalation in force, and the Capcom subscriptions on push outcomes
   and escalations.
3. The live watch as reviewer.
4. Queue drift as a source once the registry's drift check exists; the
   wake on the wrangler agent once that agent is built.

## Status

Torre Wenaus, 2026-10-23: "good plan, I like it! go". Built: the tick
(`scripts/prod-watch.py`, cron every thirty minutes) with the room's
report, live screen, recent pauses and promises as sources; the outcome
and promise writer (`scripts/watch-record.py`); the watcher session and
its brief; the wake. Escalation runs in shadow (`watch_escalated`
recorded, not live) until the tick is given `--escalate`. Not yet: the
notifier's triggers and the session's liveness as sources, the Capcom
subscriptions, the live watch as reviewer.
