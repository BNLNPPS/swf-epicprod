# Orderly drain of BNL_OSG_EPIC_PROD_1

On 22 October 2026 Torre Wenaus directed that PROD_1 be retired from new
work everywhere, while its existing tasks keep running and drain in place.
No task is moved, paused or killed by this change. New OSG work belongs
on BNL_OSG_PanDA_1.

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

The JEDI `POOLS` configuration and `regulation.CAPACITY_POOLS` continue to
count both OSG queues. Dropping the draining queue would hide real demand
and give each queue a separate generation allowance. Node exclusions and
the installed regulation settings still protect its remaining jobs.
Historical Snapper series and failure records retain the queue identity.
The room's production-site list remains as requested; its in-flight task
board still lists the draining tasks. Queue-description seeds now distinguish
the draining queue from the active production queue.

At the audit, only the two draining PCS tasks retained PROD_1 overrides;
no ProdConfig template named it. PanDA also held three older paused
background tasks (38970–38972). Those remain paused and are not resumed
or otherwise changed by the drain.

## Completion and the separate log collision

The drain is not complete while either existing production task has
remaining work, while jobs are active or finalizing, or while pilots are
still serving them. A temporary zero queued count is not task completion.
No automation in this change sets the queue offline or removes its pilots.
After demand empties, the existing Harvester demand gate stops new pilots;
admission and probes remain closed, so routine automation cannot refill it.

During this audit task 41039 reported failed jobs although its payloads
reported successful RECO registration. The pilot log registration collided
with logs from task 40443 because the new task reused its physical name.
Task 41040 similarly reused task 40440's name. This is a distinct attempt-name
collision, not proof that physics failed or that pilot supply should stop.
It needs separate repair and prevents calling the drain healthy merely
because jobs are running. Historical logs and completed physics must be
preserved.

## Validation

The retirement, pressure-front and JEDI throttler tests pass (79 tests),
including restoration of an old front list, missing/malformed closure
settings, and refusal of probes before submission. The site-canary test
suite and swf-monitor pre-commit checks pass. The drain's two PanDA tasks
and the three older paused tasks were read, not mutated.
