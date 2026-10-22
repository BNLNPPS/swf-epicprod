# Production regulation audit and corrective work, 22 October 2026

Torre approved the recommendations following the audit of 21 October's
production dialogue, regulation code and installed configuration. The work
covers task admission, JEDI job generation, pilot supply and the evidence
used to judge those controls. Implementation is in progress; the completed
record below will distinguish deployed changes from remaining work.

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

At 300 starts per minute, an 80-second nominal observation and submission
budget consumes 400 waiting pilots. A buffer of 600 adds 200 reserve.
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

Deployment verification is underway. Changes implemented:

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

