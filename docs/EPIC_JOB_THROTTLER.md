# The ePIC job throttler in JEDI

The queue-side regulator of continuous production
([CONTINUOUS_PRODUCTION.md](CONTINUOUS_PRODUCTION.md), Queue-side
regulation): a JEDI job throttler that paces job generation per PanDA
queue against what the queue is running, so that a task's jobs enter
`activated` at the rate the queue consumes them. It is the second
regulator in series with the pressure front, which admits tasks from
`ready` in priority order.

## The control point

JEDI's job generator asks the registered throttler, for each VO, source
label, cloud, work queue and resource type, whether to generate. The
answer is a throttled flag, a cap on the jobs generated in the pass, and
a minimum task priority when generation is limited to the
highest-priority waiting work. Unthrottled, the generator reads up to
`nFiles` pseudo-files per task per cycle, and the jobs are activated
within minutes.

The ATLAS production throttler (`AtlasProdJobThrottler` over
`JobThrottlerBase`) decides per work queue and resource type over all
sites together. It skips generation when the queued jobs (assigned,
activated, starting, defined) exceed both a threshold times the running
jobs and a queue limit, or when a cap on running or queued jobs is
exceeded; a waiting task of higher priority than anything queued lifts
the skip, and the pass then carries that priority as its minimum. The
reading is fair where brokerage spreads a task's jobs over sites, as in
ATLAS. ePIC's tasks are pinned to one queue each, so a count over all
sites puts a saturated queue and a starved one in the same group: a
skip starves the starved one and a pass overfills the saturated one.
For the `wlcg` and `epic` VOs JEDI registers `GenJobThrottler`, which
returns unthrottled for a work queue without a share, as the ePIC work
queues are. Production tasks carry `wlcg`: the server was commissioned
with the generic JEDI plugins under that key, and the production
team's recipe and PCS's Standard Production configuration both submit
`vo wlcg`, `prodSourceLabel managed`. The `epic` VO carries the test
paths (canary probes, GPU tests, client-API test submissions).

## The ePIC engine

`EpicProdJobThrottler`, a `JobThrottlerBase` subclass beside the ATLAS
one, registered for both VOs. It keeps the ATLAS rule and the ATLAS
configuration keys and applies them per site. JEDI's site statistics
are per VO, so each registration sees its own VO's jobs at a site;
with production on `wlcg` alone that is the reading wanted, and the
`epic` limits bound the test traffic separately.

Statistics per site come from the taskbuffer's
`getJobStatisticsByResourceTypeSite`: for each computing site of the
work queue, running, not-run (assigned, activated, starting) and
defined jobs at resource-type level.

Configuration per site, from the `config` table (component
`epic_job_throttler`, app `jedi`, VO `epic`), a work-queue-wide value
as the fallback and the engine's built-in default last:

| Key | Meaning | Built-in default |
|---|---|---|
| `THROTTLE_THRESHOLD_<site>` | generate while queued ≤ threshold × running | 2.0 |
| `NQUEUELIMIT_<site>` | the queued floor below which generation always continues, and the bound when nothing runs | the ATLAS default, four bunches of 500 to 600 jobs |
| `NRUNNINGCAP_<site>` | stop generating when running exceeds it | none |
| `NQUEUECAP_<site>` | stop generating when queued exceeds it | none |
| `MODE` | `observe` (decide and log, never throttle) or `throttle` | `observe` |

A site is saturated when
`not_run + defined > max(threshold × running, NQUEUELIMIT_<site>)` or a
cap is exceeded. The sites considered are those with statistics or
with configuration.

The answer for a work queue and resource type: throttled when every
site is saturated; otherwise unthrottled, with the pass cap set to the
room of the unsaturated sites (the sum of
`max(threshold × running, NQUEUELIMIT) - queued` over them, bounded by
the ATLAS engine's per-pass maximum) and the saturated sites named, so
that task selection serves the unsaturated sites only. A pass with no
room is throttled, never uncapped. On a server whose generator does
not take the saturated-site list (below), any saturated site throttles
the work queue: an unthrottled answer would generate for that site's
tasks. The priority
valve is the ATLAS one applied per site: a saturated site with a
waiting task of higher priority than anything queued there is not named,
and the pass carries that priority as its minimum.

Task selection must skip tasks pinned to saturated sites. The selection
query filters on VO, work queue, resource type, label, cloud, status and
minimum priority and has no site clause; it gains an `excluded_sites`
parameter, `AND (tabT.site IS NULL OR tabT.site NOT IN (...))`, carried
from the engine through `JobThrottler` and `JobGenerator`. An empty list
changes nothing, so the ATLAS engines are unaffected.

The engine ignores work-queue shares: its configuration is keyed on the
site and its statistics are read per site, so the epic work queues need
no share.

The statistics are the server's pre-cached share tables, refreshed about
once a minute, while the generator asks the engine every second or so
and generates up to the pass cap each time. Read alone, one reading
would grant its whole room on every pass until the next refresh: on
2026-09-17 three tasks submitted to BNL_OSG_PanDA_1 reached 23,000
queued jobs in eight minutes against a limit of 3,123, the first
minute's passes each reading 748 queued and the later ones passing on
BNL_PanDA_1's room because the generator ignored the exclusion. The
engine therefore keeps a ledger of grants: each pass charges its cap to
the open sites against their current reading (running, queued), and a
later pass reading the same numbers counts those grants as pending
until the reading changes or the grants are 180 s old. The ledger is a
JSON file locked with `flock`, shared by the generator processes:
`/var/log/panda/panda-EpicProdJobThrottler.ledger.json` (the system
temporary directory when that directory is not writable). The ATLAS
engine's lack-of-jobs flag, which releases the process lock so several
generators fill in parallel, is not raised: one generator at 300 jobs a
pass fills a queue faster than any of ours consumes.

## The engine as built

The engine is `swf_epicprod/jedi/EpicProdJobThrottler.py`, a
`JobThrottlerBase` subclass; its decision is the pure function
`swf_epicprod/jedi/epic_job_throttler.decide`, tested in
`tests/test_epic_job_throttler.py` without a server. It reads the
per-site statistics through `getJobStatisticsByResourceTypeSite` at the
resource-type level, the limits through `getConfigValue` (component
`epic_job_throttler`, app `jedi`, VO `epic`; `<TAG>_<site>`, then the
work-queue-wide `<TAG>`, then the built-in default), and applies the
rule above. It logs every site's reading and its answer to
`panda-EpicProdJobThrottler.log`.

`MODE` is `observe` unless the row says `throttle`. In `observe` the
engine logs what it would do and answers unthrottled, which is the
answer the server gives today; in `throttle` it returns the decision,
sets the pass cap the generator reads, charges it to the ledger, and
exposes the saturated sites as `excluded_sites`.

Registration in `panda_jedi.cfg`, section `[jobthrottle]`:

    modConfig = wlcg:any:swf_epicprod.jedi.EpicProdJobThrottler:EpicProdJobThrottler,epic:any:swf_epicprod.jedi.EpicProdJobThrottler:EpicProdJobThrottler

The configuration rows (component `epic_job_throttler`, app `jedi`)
are keyed by VO as well: `MODE` and the limits for `wlcg` govern
production; the `epic` rows govern the test traffic.

The engine needs `swf_epicprod` importable by JEDI's interpreter and, for
the site exclusion to take effect, the generator change of the previous
section; without it the generator applies the pass cap and the
throttled answer and ignores the saturated-site list. The engine reads
at import whether the installed task buffer's
`getTasksToBeProcessed_JEDI` takes `excluded_sites` and, when it does
not, throttles the work queue on any saturated site (the decision's
`exclusion_honored`); the server upgraded on 2026-09-16 (master
8f155ac9) does not, so production on BNL_OSG_PanDA_1 pauses every
other site of the `wlcg` work queue while it is saturated. The
generator change lifts that. The priority valve is not in this engine: the
ATLAS engine reads the highest queued priority from `JOB_STATS_HP`,
which the ePIC server does not populate, and the waiting-task peek is
not site-aware; a site-aware read of both is a server change for the
same upgrade.

## Interaction with the front

The front admits whole tasks; the engine paces their generation. With
both in place the activated pool at a queue is bounded by
`max(threshold × running, NQUEUELIMIT_<site>)` whatever the front
submits, and the front's set point moves from runnable depth to
committed depth: the tasks in JEDI whose inputs are not yet all
generated are the buffer the engine draws on. The census gains, per
site, the unprocessed inputs of the queue's tasks (`nFilesToBeUsed -
nFilesUsed` over their datasets), which the front's committed depth
includes; the job cap and the oversize hold of the front's first phase
retire.

`NQUEUELIMIT_<site>` set at the queue's running ceiling, read from the
census, keeps one fill of the queue activated, a few hours of work; the
threshold of 2 keeps the pool at twice the running count when the queue
is full; the front's `h_high` of one day bounds what is committed in
JEDI, generated or not.
