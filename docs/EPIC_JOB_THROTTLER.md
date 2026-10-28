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
queues are. The server was commissioned under `wlcg`; those existing
tasks retain their VO. New submissions use `epic`, with
`prodSourceLabel managed` for production and `test` for test traffic.
Both VO names remain supported by the server plugins and maintenance.

## The ePIC engine

`EpicProdJobThrottler`, a `JobThrottlerBase` subclass beside the ATLAS
one, registered for both VOs. It keeps the ATLAS rule and the ATLAS
configuration keys and applies them per site. JEDI's site statistics
are per VO; the engine sums equivalent work queues from `epic` and
`wlcg` before deciding, so old and new production share one allowance.
Production and test queues remain separate.

Statistics per site come from the taskbuffer's
`getJobStatisticsByResourceTypeSite`: for each computing site of the
work queue, running, not-run (assigned, activated) and defined jobs,
summed over every resource type. A starting job counts as running,
unlike in the ATLAS engine: a job is recorded running late and spends
most of its working life as starting (on
2026-10-05, the median finished job spent 87 of 115 minutes starting
at BNL_OSG_EPIC_PROD_1 and 4.9 of 5.1 at UM_GREX_PanDA_1, which the
engine read as 0 running). A site's queue is one
pool whatever the resource type of the jobs in it, and the generator
asks once per resource type: read per resource type, the MCORE pass saw
NERSC_Perlmutter_epic empty while the SCORE pass held it saturated, and
passed uncapped (2026-09-18, 140,000 jobs in 30 minutes). A pass that
finds no site with jobs at all is charged to a standing reading at the
default floor, never uncapped.

Configuration per site, from the `config` table (component
`epic_job_throttler`, app `jedi`, canonical VO `epic`), a work-queue-wide value as the fallback and the
engine's built-in default last; `scripts/jedi-throttler-config.sh`
reads and sets the rows:

For both calling VOs, each setting comes from the `epic` site override,
then its wide setting, then the corresponding legacy `wlcg` values.
This includes MODE and POOLS. Tune `epic`; legacy work follows the same
policy. A missing or failed statistics read for either equivalent work
queue holds generation rather than treating its workload as zero.

| Key | Meaning | Built-in default |
|---|---|---|
| `THROTTLE_THRESHOLD_<site>` | generate while queued ≤ threshold × running | 2.0 |
| `NQUEUELIMIT_<site>` | the queued floor below which generation always continues, and the bound when nothing runs | the ATLAS default, four bunches of 500 to 600 jobs |
| `NRUNNINGCAP_<site>` | stop generating when running exceeds it | none |
| `NQUEUECAP_<site>` | stop generating when queued exceeds it | none |
| `POOLS` | queues that feed one set of slots, read as one: `pool:queueA,queueB;pool2:...` | none |
| `MODE` | `observe` (decide and log, never throttle) or `throttle` | required explicitly; absent/invalid holds generation |

**Pools.** Queues that feed one set of slots are one reading: the
summed counts of the pool's queues, held to the pool's own limits
(`NQUEUELIMIT_<pool>` and the rest), with every queue of the pool
excluded or granted together, whether it has jobs yet or not. The OSG
pool is `osg:BNL_OSG_PanDA_1,BNL_OSG_EPIC_PROD_1` with
`NQUEUELIMIT_osg` 3123 since 2026-10-21. Read per queue, each OSG queue
had its own floor (3,123 and the default 2,000) and its own share of the
running count, so the pool's allowance was the sum: on 2026-10-21 the
pool held 20,030 queued jobs against 1,121 running. A pool member's own
limit rows are not read.

The counts are per JEDI work queue: production runs in `managed`, the
canaries and test runs of both VOs in `test`, and each work queue is
read and held on its own. A canary is therefore not held back by
production saturation at its queue. Both VOs throttle since 2026-10-21;
the `epic` row had been left at `observe` when `wlcg` was switched on
2026-10-20.

`running` counts `running` and `starting`: a pull pilot's job is
`starting` for seconds before it runs. Jobs left `starting` with no
pilot inflate it; on 2026-10-21 500 such jobs of task 40855, fetched in
push mode on 2026-10-19 just before GREX went to pull, were killed.

A site is saturated when
`not_run + defined > max(threshold × running, NQUEUELIMIT_<site>)` or a
cap is exceeded. The sites considered are those with statistics or
with configuration.

The answer for a work queue and resource type: throttled when every
site is saturated; otherwise unthrottled, with the pass cap set to the
room of the unsaturated sites (the sum of
`max(threshold × running, NQUEUELIMIT) - queued` over them, bounded by
the ATLAS engine's per-pass maximum) and the saturated sites named, so
that task selection serves the unsaturated sites only. A site exactly at
its bound, with no room but not over it, is named with the saturated
ones: another site's room is never spent on its tasks. On 2026-10-21
UM_GREX_PanDA_1 read room 0 (queued 2,814 of 2,814), was left off the
list, and the generator made 4,999 jobs for each of two GREX tasks. A
pass with no room is throttled, never uncapped.

A throttled answer stops a pass only where JEDI locks its generation
cycles: the generator skips a throttled work queue only when it holds
the process lock, and takes the lock only for the VOs that
`[jobgen] lockProcess` names in `panda_jedi.cfg`. Without that key the
answer was ignored and the pass ran uncapped, with only the excluded
sites kept out. The ePIC server sets `lockProcess = wlcg|epic:any:any:any`
since 2026-10-21 (distcomp-services panda-server CHANGES.md).

A saturated site never holds the whole work queue. When every site
with jobs is saturated, the pass goes on for the sites with no jobs yet
(a new task at a quiet site), capped at the default floor of a standing
reading charged in the ledger like a site. On a server whose generator
does not take the saturated-site list (below), the pass still goes on,
capped at the open sites' room, and the generator may fill a saturated
site's tasks within that cap. The rule that held the whole work queue
instead, `HOLD_QUEUE_ON_SATURATED_SITE`, is off since 2026-10-05: with
the mode at `throttle` it held every production pass at every site
from at least 2026-09-26 while UM_GREX_PanDA_1 alone was saturated. The priority
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

`MODE` must explicitly be `observe` or `throttle`; missing or invalid values
return a temporary error and hold generation. Throttle mode also requires
the actual JEDI generation-lock configuration and site-exclusion support.
The ledger reserves under one lock across reading, decision and durable
write, using atomic replacement. Malformed or unwritable ledgers hold
generation. Keys name the logical work queue, shared by both VOs and
all resource types. Production and test queues have separate reservations.
Successful enforced decisions emit `REGULATION_OK` runtime evidence. The
server reporter supplies that evidence to the front, which requires fresh
successful decisions for both VOs as well as the effective canonical throttle mode
before trusting `front.jedi_throttled`.

Registration in `panda_jedi.cfg`, section `[jobthrottle]`:

    modConfig = wlcg:any:swf_epicprod.jedi.EpicProdJobThrottler:EpicProdJobThrottler,epic:any:swf_epicprod.jedi.EpicProdJobThrottler:EpicProdJobThrottler

The configuration rows (component `epic_job_throttler`, app `jedi`)
remain keyed by VO for compatibility. The `epic` rows govern both names;
legacy `wlcg` values are fallbacks only. The active OSG pool contains
BNL_OSG_PanDA_1; the retired BNL_OSG_EPIC_PROD_1 has its own draining
pool, preserving its isolation from active production.

The engine needs `swf_epicprod` importable by JEDI's interpreter and, for
the site exclusion to take effect, the generator change of the previous
section; without it the generator applies the pass cap and the
throttled answer and ignores the saturated-site list. The engine reads
at import whether the installed task buffer's
`getTasksToBeProcessed_JEDI` takes `excluded_sites` (the decision's
`exclusion_honored`); the server upgraded on 2026-09-16 (master
8f155ac9) does not, so a saturated site's tasks can receive jobs
within each pass's cap until the generator change is in. The priority valve is not in this engine: the
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
