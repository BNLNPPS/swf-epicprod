# Orderly drain of BNL_OSG_EPIC_PROD_1

On 22 October 2026 Torre Wenaus directed that PROD_1 be retired from new
work everywhere. The initial in-place drain was superseded that evening
by an instruction to move its remaining work to BNL_OSG_PanDA_1, preserving
already-running jobs. Both task moves were verified by 22:38 UTC. No
unstarted production backlog remains on PROD_1; its running jobs finish
there naturally. New OSG work belongs on BNL_OSG_PanDA_1.

## Why it returned

Tasks 41039 and 41040 were explicitly queued whole-task reruns that kept
their old PCS placement. The front executed those requests at 18:58 UTC,
although automatic feeding of the queue was off. Removing the queue from
the floor did not close admission, and closing PCS admission afterward
did not cancel tasks already submitted to PanDA. Those tasks generated
jobs and Harvester correctly supplied pilots for them.

## Admission and the drain

- The live front list excludes PROD_1 and its feed switch is off. The
  existing SysConfig closure remains. `regulation.RETIRED_QUEUES` now
  preserves that closure even if an older configuration is restored.
  The fallback front list names the active OSG queue.
- The submission doer rechecks its final destination before creating
  datasets or submitting a task, covering queued requests, trials and
  payload canaries. PCS place, submit, rerun, queue, trial and move use
  the same retirement through `front.closed_queues()`.
- PROD_1's six-hour automatic probe schedule was disabled through the
  authenticated probe API. The host supplies permanent closures to
  site-canary, which refuses both scheduled and manual new probes on
  those queues. Collection of existing probes continues. The standalone
  landing-probe default now names the active queue.
- The GitHub schedconfig override sets PROD_1 to `brokeroff`. The installed
  GenJobBroker on pandaserver01 checks non-online status only for tasks
  without a preassigned site. Existing pinned tasks therefore retain job
  generation. Harvester permits provisioning for `brokeroff` queues.
  The existing five-minute writers propagate the override; this change
  requires no PanDA service or listener restart.
- The live Harvester configuration remains in pull mode with 600 waiting
  pilots, up to 300 new pilots per cycle, and
  `noPilotsWhenNoActiveJobs=true`. That gate caps waiting demand by
  activated jobs and stops replenishment at zero. Existing pilots finish
  naturally; none is removed by this change.

## References that remain deliberately

`regulation.CAPACITY_POOLS` continues to count both OSG queues for actual
resource use. Production JEDI generation now separates them:
`osg:BNL_OSG_PanDA_1;osg_drain:BNL_OSG_EPIC_PROD_1`. The shared generation
reading let PROD_1's backlog suppress refill of the active queue. The
22:16 UTC configuration change restored refill while the moves completed;
with both production tasks moved, the retired queue has no production
task to generate new work. Node exclusions and the installed regulation
settings still protect its remaining running jobs.
Historical Snapper series and failure records retain the queue identity.
The room's production-site list remains as requested; its in-flight task
board still lists the draining tasks. Queue-description seeds now distinguish
the draining queue from the active production queue.

At the initial audit, only the two draining PCS tasks retained PROD_1
overrides; their soft moves also changed those overrides to the active
queue. No ProdConfig template named PROD_1. PanDA also held three older paused
background tasks (38970–38972). Those remain paused and are not resumed
or otherwise changed by the drain.

## Completion and the separate log collision

The queue drain is not complete while its jobs are active or finalizing,
or while pilots are still serving them. The moved tasks complete on the
active queue; a temporary zero queued count is not task completion.
No automation in this change sets the queue offline or removes its pilots.
After demand empties, the existing Harvester demand gate stops new pilots;
admission and probes remain closed, so routine automation cannot refill it.

During this audit task 41039 reported failed jobs although its payloads
reported successful RECO registration. The pilot log registration collided
with logs from task 40443 because the new task reused its physical name.
Task 41040 similarly reused task 40440's name. This is a distinct attempt-name
collision, not proof that physics failed or that pilot supply should stop.
The [ePIC adder](EPIC_ADDER.md), installed at 22:34 UTC, now omits absent
and failed files from Rucio registration. Fresh job 5805903 finished with
`logHeld=s3` and DDM error 0 after the restart. Historical logs and completed
physics are preserved. The reused task names are repaired separately in
PCS allocation; no existing task is renamed.

## Validation

For the initial retirement deployment, the retirement, pressure-front
and JEDI throttler tests passed (79 tests),
including restoration of an old front list, missing/malformed closure
settings, and refusal of probes before submission. The site-canary test
suite and swf-monitor pre-commit checks passed. The drain's two PanDA tasks
and the three older paused tasks were read only at that stage; the later
authorized moves are recorded below.

## Deployment and observed state

The normal full deploy completed at 20:55:09 UTC on 22 October, release
`branch-infra-baseline-v46-20261022-165232`: swf-epicprod `96f902f`,
swf-monitor `1a6f34c`, and site-canary `ab037f8`.
[Deployment run](https://github.com/BNLNPPS/swf-epicprod/actions/runs/39852567850)
finished successfully, verified the shipped files, and returned health HTTP 200.
Deployed imports confirmed the permanent closure, disabled probe policy,
active OSG admission, and both queues retained in the capacity pool.

The existing writers on pandaserver01 and osgsub01 installed the `brokeroff`
override at 20:55:02 UTC from GitHub revision `96f902f`; their provenance
records identify that revision. No PanDA server or Harvester service was
restarted. The mirrored PanDA schedconfig was confirmed `brokeroff` at
20:57:35 UTC after its normal configurator refresh; the admission guards
were already live independently of that refresh.

The live descriptions of PROD_1, active OSG, and the earlier direct BNL
queue were corrected through the authenticated queue-description endpoint.
That endpoint first returned HTTP 500 because its existing implementation
omitted the Django `timezone` import. The one-line fix, swf-monitor
`72b0485`, passed pre-commit checks and went live through a lightweight UI
deploy at 20:55:36 UTC. All three description updates then returned HTTP 200.

The post-deploy task read still showed 41039 running and 41040 ready; the
three old background tasks remained paused. At 20:56 UTC, 41039 had 1,141
running jobs and 5,975 activated jobs. At 20:57 UTC, all 485 archived failed
jobs inspected for 41039 had payload exit 0, `payloadRegistration=registered`,
`logHeld=s3`, and DDM error 200. These current job records establish the log
registration failure separately from physics delivery. A normal log lookup
can return the old October 2 tarball because of the collision; such a tarball
is not evidence for the new job. No log, task, job, or pilot record was
rewritten to hide or bypass the failure.

## Remaining work moved to active OSG

At 22:17 UTC, authenticated PCS soft moves were requested for tasks 41039
and 41040. PanDA accepted both. JEDI closed their unstarted jobs before
changing the task destinations; it left running jobs to finish. The
operations agent then verified the new sites and settled the attempts
consumed by those closures. Both operations were `verified` by 22:38 UTC:
41039 `running`, 41040 `ready`, both at BNL_OSG_PanDA_1. PROD_1 had zero
activated jobs, 808 running and one holding; active OSG had 3,395 activated
jobs. The operation IDs are `8d16c648-ec83-440d-b036-61ed384f03b0` and
`2fcdafe7-09c2-4d93-ad87-81fc3474140c`.

The pool split restored Harvester's configured 600 waiting pilots. This
is supplied demand, not a promise of available execution slots: a Condor
analysis of waiting pilot 26364.0 at 22:36 UTC found only one willing
matching slot. Running-job recovery must therefore be watched separately
from generated stock and pilot supply.

## Overnight watch and worker-loss reports

A read-only user service, `epic-osg-watch-20261022.service` on swf-testbed,
samples the two queues every minute through 08:00 ET on 23 October. Its
script and JSON snapshots are in
`/data/wenauseic/swf-epicprod/osg-watch-20261022/`. It checks retired-queue
admission and backlog, generated stock against ungenerated work, waiting
pilots, running-count decline and ten-minute DDM/worker-loss outcomes.
Changed conditions and ten-minute summaries notify the owning swf-3
session through TeamComms; delivery into that session was verified at
22:47 UTC. It changes no production records or controls.

At 22:47 UTC, active OSG had 3,300 activated jobs and 600 waiting pilots;
PROD_1 had no queued jobs. The last ten minutes recorded 389 finished
jobs, no DDM failures and 121 worker-loss reports. Those loss reports
must be dated from their batch events, not their later PanDA finalization:
worker 26350.7 was evicted from `uct2-c507.mwt2.org` at 16:56 ET and its
orphaned job was reported failed only at 18:42 after the restarted worker
ended elsewhere. Three additional workers reported at about 18:47
(26269.0, 26346.5 and 26338.37) likewise had their evictions at 16:26–17:24,
with reason `Startd was shutdown`. These samples establish delayed
reporting of earlier site evictions; they do not rule out future losses.
