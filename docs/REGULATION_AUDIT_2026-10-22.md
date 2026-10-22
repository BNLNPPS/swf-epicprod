# Production regulation audit and corrective work, 22 October 2026

Torre approved the recommendations following the audit of 21 October's
production dialogue, regulation code and installed configuration. The work
covers task admission, JEDI job generation, pilot supply and the evidence
used to judge those controls. The code and configuration corrections are
implemented and deployed. This record separates verified controller behavior
from performance targets that remain unproven, including cold-start latency
and capacity the external pool has not offered.

## OSG policy

The starting objective is to support 300 pilot starts per minute per
backlogged queue when the pool offers slots, including a transition from
an empty queue. This is a design objective, not a measured pool guarantee.

| Control | Previous setting | Accepted setting |
|---|---|---|
| Waiting pilots | 30% of running, minimum 200, maximum 300 | 600, bounded by activated work |
| Queue lookup | 60 seconds | 20 seconds |
| New workers per cycle | 100 | 300 |
| Worker monitor | 1800 seconds; PanDA_1 override 300 | 30 seconds |
| Condor status cache | 300 seconds | 30 seconds |
| Job-demand cache | 10 minutes | 30 seconds, independently of static records |
| Pilot timefloor | 120 minutes | retained |
| History eligibility | 7200 seconds | 86400 seconds |

For replenishing an already busy queue, the nominal observation and
submission budget is 30 seconds of Condor cache, 30 seconds of monitor
latency and 20 seconds of submission eligibility. At 300 starts per minute,
that 80-second budget consumes 400 waiting pilots. A buffer of 600 adds 200 reserve.
The controller cannot guarantee pool slots or a start rate. With 15-minute
jobs, the two-hour timefloor reduces steady pilot replacement demand by
roughly a factor of eight, while cold starts still need new pilots.

`scripts/configure-osg-regulation.py` validates the installed files before
writing, keeps dated backups and changes no worker or job status. Four
small, version-checked compatibility patches let the existing Harvester
cacher refresh only job statistics rapidly, apply the operational monitor
cadence to workers carrying historical queue configurations, and extend
history eligibility for those workers too. Missing or older-than-90-second
job demand holds new pilot submissions. The reference Harvester
checkout is unchanged. An upstream upgrade must retain or replace these
patches; incompatible source makes the installer stop before writing.

The existing `panda_harvester-uwsgi` unit is the service activation path.
Queue configuration, service activation and observed controller behavior
are separate verification steps.

## Implementation record

The following changes are live:

- JEDI reserves grants atomically under a shared lock, scopes them by VO and
  work queue, and holds generation on corrupt ledgers, invalid modes or
  missing locking/exclusion support. The front requires fresh runtime
  enforcement evidence from both VOs.
- The front and census count OSG capacity and workload once per shared pool,
  omit paused inputs, detect stalled delivery after 15 minutes even with a
  deep buffer, skip oversized candidates, and exempt only failures matching
  a finding's mechanism or exit code and stage.
- The shadow pilot controller converts GREX's pilot target to worker units,
  measures starts directly, and proposes worker limits appropriate to pull
  mode. It remains shadow-only.
- Pilot health exposes missing protection as amber when work waits. The
  scheduled notifier evaluates supply independently of the browser, warns
  about a 25 percent running-count fall sustained for 15 minutes with a
  backlog, and detects zero-running starvation after five minutes.
- Static records synchronize the GitHub overrides onto the server and both
  harvesters, with commit and content hashes. GREX/JLab shipped pilot data
  also follows the merged records. OSG requests and advertises 4096 MB;
  GREX advertises the one-core, 6000 MB job unit separately from its fixed
  eight-core, 48000 MB allocation. Google launching is disabled and now
  consults the front closure policy.

The OSG memory correction excludes 187 advertisements between 3700 and
4096 MB/core in the 04:51 UTC raw pool sample (1542 at the old threshold,
1355 at the new one). These counts precede admission and availability
filters. Existing workers drain naturally with their original requests.

Existing node guard remains distinct from supply regulation. Admission
holds do not pause running tasks or suppress all their retries. Automatic
storage-outage task pausing needs its own defined action policy; no new
pause/kill/reassign operation is introduced here.


## Deployment and evidence

All times below are UTC on 22 October (subtract four hours for ET).

| Component | Repository revision / activation |
|---|---|
| Initial OSG policy and compatibility installer | swf-epicprod `0e19863`; first activation 04:35 |
| Front, JEDI, pilot decisions, synchronization and notices | swf-epicprod `ffd9fdf` |
| Census, failure matching, reporters and pilot health | swf-monitor `f198e07` on `infra/baseline-v46` |
| Missing-count and stale-demand health protection | swf-monitor `9ed3656`; swf-epicprod `e761885` |
| First full monitor/ops deployment | `branch-infra-baseline-v46-20261022-005428`, healthy 04:56:46 |
| Final full monitor/ops deployment | `branch-infra-baseline-v46-20261022-010609`, healthy 05:08:27 |
| GREX sparse-log health correction | swf-monitor `fdb6afd`; lightweight UI/MCP deployment completed 05:18:45 |
| JEDI package on pandaserver01 | wheel built from `ffd9fdf`; service active 04:56:27; `pip check` clean |
| All three static record writers | installed 04:56; synchronized input revision `6671257` |
| PanDA installation change log | distcomp-services `2726180` |
| WFMS operations guide | epic-wfms-docs `19eb931` |

The full deployment returned a fresh snapshot with no errors; OSG demand
evidence was 59 seconds old and both independent waiting counts remained
600. After the subsequent health correction, the fresh snapshot again had
no errors: OSG had 597 Harvester waiting versus 596 schedd idle under the
600 limit, and GREX had 590 waiting with its last applied limit of 590.

The first activation exposed a real configuration parsing error: Harvester
parsed the one-line per-key refresh map as text. The installer now writes
the multiline JSON its parser requires and is tested for repeatability.
This was corrected at 04:45:41 before claiming the cache cadence worked.

Measured results:

- **Refill:** 415 OSG pilots were submitted from 04:45:52 to 04:46:06,
  a 14-second span. This replenished the former roughly 200-pilot buffer.
  It measures submission, not slot acquisition, and is not a cold-start
  injection benchmark.
- **Actual supply:** the 04:58 snapshot read 600 waiting in Harvester,
  an effective limit of 600, and 600 idle in the schedd. The empty
  EPIC_PROD queue had zero waiting pilots and zero activated demand;
  its adjuster applied `min(0, 600)` and submitted none.
- **Worker observation:** 9,861 repeated worker checks after the 04:56
  activation had a median gap of 24.327 seconds, 95th percentile 31.762,
  and maximum 43.261. Checks can occur sooner through other monitor paths.
  These are observed timings, not a hard real-time guarantee.
- **Demand:** both job-statistics caches advanced every approximately
  30 seconds in the live log (04:46:44, 04:47:15, 04:47:45, 04:48:15).
  Static records remained on their separate cadence. Newly activated work
  is therefore no longer hidden for a ten-minute Harvester cache period.
- **History:** 18 OSG workers ended as `finished` in the ten-minute
  observation window, replacing the earlier all-cancelled observation.
  No existing worker or task was killed to obtain this result.
- **JEDI:** both VOs emitted fresh successful enforced decisions after
  restart. The reporter delivered them, and the front evaluated its
  regulator gate green. The front cycle at 05:03:04 completed in 8.2
  seconds with no errors.
- **Shared pressure:** the front used one OSG ceiling of 4,268, counted
  once in total capacity, and one combined committed depth for both
  member queues. PanDA_1 showed `not_pulling`; EPIC_PROD retained its
  existing feed-off setting. A full buffer no longer hides the delivery
  warning.
- **Failure scoping:** the live census counted 953 GREX failures and
  separately exempted four matching f-15 failures in the sampled gate
  window; task identity alone no longer exempted the other failures.
- **Pilot units:** the live GREX shadow record converted the configured
  1,000-pilot target to 125 workers. It read 118 running workers and
  used one actual worker start in 300 seconds, or 12 starts/hour.
- **Synchronization:** manual installs and the normal 05:00 cron runs
  on pandaserver01, pandaharvester01 and osgsub01 produced identical
  overrides and output hashes. Overrides SHA256:
  `66f1b7d324781f107aecdeade6155a5649acb90e6a52f3d0ac83e0964dad9980`.
  Output SHA256:
  `bb9c18603ade662612ce4189eaecd7a5c4ef2b8d8169c2d53365b1c93d830027`.
- **Independent evaluation:** the normal production-notifier cron ran
  at 05:02:18 and maintained supply/decline state for all nine queues.
  The focused decline test covers 1,000 to 433 running while 5,000 jobs
  wait, including deduplication and recovery notification.
- **Sparse-log correction:** at about 05:10, the notifier exposed a defect
  in my first health change: it expired GREX's last applied limit after
  15 minutes even though the report was fresh. swf-1 identified that
  Harvester omits this log line while the waiting queue is full. The fix
  retains the event value and timestamp, labels it "last applied limit,"
  and checks report freshness and log-read success separately. The deployed
  snapshot now reports 590 correctly from the 04:52:18 event. A limit never
  observed is still unknown; a stale report still shows degraded protection.

The pool still limits delivery: at 04:59:50 the idle analysis found zero
willing slots among 3,527 considered. The recent observed starts were
roughly one to six per minute. The 300-starts/minute design objective,
and a full zero-to-busy production injection, have not been demonstrated
under a newly available slot burst. The verified result is rapid submission
and accurate replenishment at the current demand and availability.

### Independent watch through 05:24 UTC

swf-1 supplied a separate five-minute watch covering 03:40–05:24 UTC,
using the Harvester adjuster log and public snapshot (TeamComms message
`7c2e4b91-d8a3-4f56-a0e7-3b9f1c6d2e85`). These are peer observations,
not a second measurement made by this session:

- Before the coordinated change, the queue-level 300-second monitor
  override alone showed no effect. Harvester's running count held at
  567 during 03:38–03:59, then 414 during 04:02–04:25, with updates at
  04:02 and 04:31. The watch did not isolate why that override failed.
- After approximately 04:49, Harvester waiting stayed at 597–600 and
  schedd idle at 596–600. Harvester's running count changed between
  samples: 338, 326, 322, 316, 308 and 310.
- Pool starts remained around two per minute throughout, with
  `opened_1h` between 75 and 148. Reported running declined from 365
  at 04:14 to 319 at 05:23 despite roughly 600 pilots idle in the schedd.

This corroborates repaired accounting and a maintained waiting buffer.
It does **not** demonstrate increased pool delivery: the binding constraint
during this watch remained slot acquisition. Increasing the waiting cap
again is not justified by these observations alone.

The scheduled notifier subsequently emitted `regulation.recovered` for
GREX, reporting 919 running jobs and 2,334 queued (notification
`5a04819b-73f7-462a-9f95-4b2c5d027f87`). That confirms the false missing-limit
alert cleared through the scheduled path; it is not evidence of a change
in GREX throughput.

## Cold-start latency: an additional upstream clock

Harvester's 30-second demand cache polls PanDA statistics endpoints. Those
endpoints read `doma_panda.mv_jobsactive4_stats`, an ordinary summary table
populated by `doma_panda.update_jobsactive_stats()`, rather than querying
newly activated jobs directly. Read-only samples at 05:10:09 and 05:17:29
found source timestamps at the corresponding minute boundaries. Those
naive timestamps are Eastern time, not four-hour-old UTC values.

This minute-scale upstream refresh is additional to Harvester's cache.
At that observed cadence, allow roughly 90 seconds for newly activated
work to become visible (60 upstream + 30 cache), and roughly 110 seconds
through submission eligibility (+20), before service and scheduling delays.
These are nominal phase budgets, not hard deadlines or measured cold starts.
Task admission and JEDI job generation occur before this interval.

The audit's approximately 60-second new-demand objective is therefore
**not established as an upper bound**. The source scheduler was not changed;
its administrative configuration on pandadb01 was not accessible with the
available OS privileges. The new 90-second stale-demand guard measures
Harvester's fetch age, not the age of the underlying summary rows. A tighter
end-to-end bound needs an upstream refresh change or a direct-demand API,
followed by an empty-queue activation measurement. The 14-second warm
refill above must not be used as evidence for that cold-start objective.

## Validation and operational boundaries

Focused front, credential, atomic-ledger concurrency/corruption, GREX-unit,
GKE, finding-scope, stale-telemetry and notification tests passed. The
installer validated all patch contexts and Python syntax, preserved unrelated
settings and produced no changes on its second run. The static writer
retained its last-good output when an invalid override was supplied.
Django checks and the front table's template compilation passed. Full
deploys verify imported package files against the repository and require
HTTP 200; the production operations and canary agents reached READY.

The 120-minute timefloor is retained. At 15 minutes/job, it permits roughly
eight sequential jobs per pilot; the final job can extend beyond two hours,
which is why the old two-hour history eligibility was wrong. It reduces
steady replacement demand but does not provide pilots for a cold queue.

OSG reporters now run every minute with a lock to prevent overlap. The
scheduled notifier still runs every five minutes: its five-minute starvation
grace and fifteen-minute decline grace can each take one additional poll
to notify. OSG reports older than three minutes, missing counts, and demand
cache evidence older than 90 seconds show missing protection when work
waits. Other Harvester reports keep their existing five-minute cadence.

The GitHub synchronization updates **overrides**, not Python programs or
Harvester compatibility patches. Each writer polls every five minutes;
the server then reloads within its 200-second cycle, and Harvester's static
cache within ten minutes. Existing pilots retain the data shipped when they
started. The scripts keep last-good data on a source outage, with the last
successful revision and time in the provenance sidecar.

## Files, maintenance and rollback

- `scripts/configure-osg-regulation.py`: repeatable OSG policy and exact-context
  patch installer; `--apply` required to write. It touches installed Harvester,
  its configuration and the two pull submit templates. Reference checkouts
  remain unchanged.
- `scripts/install-schedconfig-sync.py HOST`: the retained installation
  procedure for the server and two Harvester hosts. Run as root from a
  checkout; on OSG it also activates the Harvester policy. The recurring
  writer is `scripts/panda-schedconfig.py --sync-from-github`.
- Static directories: `/opt/panda/etc/panda/schedconfig` on pandaserver01;
  `/data/atlpan/harvester_common/schedconfig` on pandaharvester01;
  `/var/data/atlpan/harvester_common/schedconfig` on osgsub01. Each has
  `schedconfig.json.provenance.json`. Cron files are `/etc/cron.d/panda-schedconfig`
  and `/etc/cron.d/harvester-schedconfig`.
- OSG original policy/module backups carry suffix
  `.before-regulation-20261022T043225Z`; the later adjuster and submit-template
  backups carry `.before-regulation-20261022T045633Z`. Restore the coherent
  original set with Harvester stopped, refresh queue configuration and
  repopulate its monitor FIFO with the native admin command, then start
  `panda_harvester-uwsgi`. Running payloads are separate from that daemon.
- JEDI backup: `/opt/panda/swf_epicprod.before-regulation-20261022`.
  To restore the old throttler, stop `panda_jedi`, restore its `jedi`
  subdirectory from that backup, then restart. The new ledger keys are
  scoped separately; do not reinterpret them as old per-site keys.
- Reporter backups are beside the shared `~/.local/bin` scripts with
  `.before-regulation-20261022`; the previous OSG crontab is
  `~/.crontab.before-regulation-20261022`.
- Revert overrides in GitHub to roll them back. A local-file-only rollback
  is overwritten at the next synchronization unless that cron is stopped.
  Revert front/monitor source through the normal coordinated deployment.

No schema migration, production task move, task kill, queue reopening,
operator-account change or curated-finding mutation was part of this work.
The payload canary and credential warning-window behavior were preserved.

## GREX follow-up: useful pilot lifetime and allocation drain

The later running-count drop exposed a separate inefficiency in GREX's
eight-pilot allocations. Once a pilot passes its intake timefloor, it finishes
its current job and exits. The allocation remains occupied until the last of
its eight pilots exits. Thus an allocation can hold eight cores while only
one job remains. Timefloor enables successive jobs, but a short intake window
still leaves a substantial final, partially occupied tail.

At 06:11 UTC the schedd had 118 running eight-pilot allocations while PanDA
reported 684 running jobs and about 2,000 activated jobs. A 06:13 allocation/job
sample found 51 running jobs in 31 allocations older than 210 minutes, versus
231 jobs in 29 allocations younger than 150 minutes. Some PanDA worker rows
lagged the actual schedd, so these instantaneous counts must not be presented
as measured lifetime utilization.

The successful-job sample over the preceding six hours was:

| Task | Jobs | Median minutes | p90 | p99 | Maximum |
|---|---:|---:|---:|---:|---:|
| 40857 | 2,155 | 68.83 | 96.03 | 120.70 | 157.82 |
| 40856 | 1,979 | 68.25 | 92.39 | 117.69 | 164.42 |

The old five-hour allocation and three-hour intake were sized for an earlier
14-minute median and 80-minute maximum. That sizing no longer matched the
workload. Inspected old-cohort logs explicitly stopped intake after
`timefloor=10800` seconds. They did not contain the floor-spreading launcher's
log message: queued workers retain the executable and data transferred when
submitted, even after the source file is replaced.

### Accepted correction

Torre approved extending new GREX allocations to twelve hours, with an
approximately eight-to-nine-hour intake window and a three-hour reserve for
the final job. The current observed maximum leaves about sixteen minutes of
headroom inside that reserve; future longer jobs are still possible. This is
a workload-based operating choice, not a guarantee that every final job fits.

| Control | Previous | New |
|---|---|---|
| Fixed allocation request | 300 minutes | 720 minutes |
| Allocation cores / memory | 8 / 48000 MB | unchanged |
| Shipped pilot `maxtime` | 18000 seconds | 43200 seconds |
| Shipped `timefloor` | 180 minutes | 510 minutes |
| Spread | up to ±30 minutes in newer launcher | retained, then bounded |
| Final-job reserve | depended on floor and job duration | at least 180 minutes |
| Startup allowance | no common deadline | 5 minutes |

`deadline-v2` chooses an allocation intake floor of 480–535 minutes after
applying the spread and reserve. All eight pilots receive time remaining to
the same allocation deadline and intake deadline; staggered starts cannot
reset either clock. The launcher bounds the budget by both the submit
environment and shipped queue data, so mixed old/new files cannot extend a
short allocation. Missing or invalid policy stops before starting wrappers.
The longer useful phase should amortize the final tail; it does not eliminate
it or establish an observed utilization improvement yet.

JLab explicitly retains its previous 180-minute floor instead of inheriting
GREX's new value through queue cloning. OSG's 120-minute floor is unchanged.
The GREX front feed switch remains as it was: there was already ample job
backlog, and automatic placement of additional tasks would not fix this drain.

### Installation and rollout evidence

- Launcher and repeatable installer: swf-epicprod `0072324`.
- Queue policy and operating documentation: swf-epicprod `73495e3`.
- WFMS operations guide: epic-wfms-docs `9749d11`.
- On pandaharvester01, the installer replaced the launcher first and then
  the submit template at **06:44:48.863440 UTC**. Backups beside both files
  have suffix `.before-grex-lifetime-20261022T064448.863440Z`.
- The revision-pinned writer synchronized GREX's shipped pilot data at
  **06:52:42 UTC** from `73495e3`: `timefloor=510`, `maxtime=43200`.
  JLab remained `timefloor=180`, `maxtime=86400`.
- The ordinary **06:55 cron cycle** synchronized all three hosts to
  `73495e3`, with identical overrides SHA256
  `a108a058187d60785666fb8bbb34b178252c1ad629a188222a00b046712c861c`
  and merged-output SHA256
  `165e22e15be208304f62fb8d66d316551146def65b03c30faae2ea3a4de6c2a5`.
  Both the server and OSG's local static records now carry GREX's updated
  lifetime too; this verifies the normal GitHub synchronization path.
- At 06:53, the installed launcher hash matched the committed file:
  `78afad625c7a5343c44c3e680e956bd26dbc9a9fe55104687a6bba73b06b6794`.
  The installed submit template requested 720 minutes, eight cores and
  48000 MB. Harvester reads this template for each submission; no restart
  was needed.
- The first automatic app deployment of `0072324` refused because another
  authorized session was editing PCS files. That session's `18820b3` UI
  deployment succeeded at 06:45:54; the pilot files were separately installed
  as described above. The `73495e3` deployment completed at 06:52:12 with
  scope `none`, as intended for GitHub-polled configuration and documentation.
- At 06:53, all 591 idle and 118 running GREX workers still requested
  300 minutes. Installation alone therefore did not demonstrate acceptance
  or execution of a twelve-hour worker. No queued workers were removed to
  force the new policy to the front. At 06:55 there were 584 idle and 118
  running, still all under the old request.

The six focused launcher/installer tests passed, including actual subprocess
execution, mixed-version budgets, staggered starts sharing one deadline,
invalid-policy rejection, preserved unrelated submit settings and repeatable
installation. Four regulation tests and shell syntax checks also passed.

The actual GREX partition routing and maximum walltime for this eight-core
request remain unverified. Peers reported Wouter's earlier four-day limit
for a one-core partition; that does not establish the eight-core route's
limit. A longer request can also reduce backfill opportunities. New-cohort
CE acceptance, start latency and full-lifetime occupied-core efficiency must
be distinguished from installed settings. Existing old workers drain naturally.

The PanDA jobs inspected advertise `maxWalltime` of five hours or four days.
Enabling remaining-time dispatch filtering would reject most of that existing
backlog rather than select jobs using their measured durations. That option
was therefore left disabled; job metadata was not rewritten to manufacture a
fit.

For rollback, restore the shorter GitHub queue policy and synchronize the
shipped data, then restore the submit template from its dated backup. The
guarded launcher can remain: its minimum-of-budgets rule also protects a
shorter allocation. A local-only queue-data rollback would be overwritten
by the next GitHub synchronization. Neither direction changes already-spooled
workers.

### Decline notification correction

The notice reporting recovery at 684 running jobs after a 916-to-670 decline
did not establish recovered capacity. The rolling one-hour peak could age
out and clear the condition by moving its own reference downward. In
`0072324`, the notifier retains the incident peak until the decline condition
actually clears. Its regression test ages the original peak out while jobs
remain at 433 of 1,000 and verifies that no recovery is emitted. The existing
25-percent threshold and backlog condition are unchanged. The scheduled
notifier reads this script directly; no service restart was required.
