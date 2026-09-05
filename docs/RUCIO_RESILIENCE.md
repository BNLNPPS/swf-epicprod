# Preventing Rucio registration losses

## Example of the failure type to prevent

On 2026-08-31 a synchronized wave of about 4,800 Perlmutter jobs, started within six minutes when the batch system granted a block of allocations at once, reached the output-registration step together roughly ninety minutes later. The JLab Rucio server shed the excess load — auth handshake timeouts concentrated on the x509 proxy endpoint, dropped connections, and failed registration transactions — and about 4,400 jobs failed at their final step after completing their payload work, roughly 6,500 core-hours of finished processing lost to bookkeeping. Concurrent registrations from other sites succeeded throughout: this was saturation under coherent load, not an outage.

With continuous pressure-driven submission beginning for the 26.09 campaign, registration failure must stop costing completed payload work.

## Measure 1 — de-synchronize the load

- Stagger allocation fills at the harvester so job waves do not start, and therefore do not finish, in lockstep.
- Add a randomized delay (minutes-scale) before the payload's registration step, and registration retry with exponential backoff and jitter, in the campaign run script and the PCS runner as one contract. The in-script retry is interim: once Measure 2 lands, the in-job attempt is single and retries live in the registrar.

This flattens the pulse and likely prevents recurrence at current scale. It does not protect against genuine Rucio degradation.

## Measure 2 — decouple registration from job success

- The job uploads its output (the transfer path held throughout the incident), then makes one jittered registration attempt. On success the catalog is as current as today. On failure the job records the pending registration in its job report and still exits success on good physics plus completed upload — registration failure never fails a job.
- An asynchronous registrar under the production operations agent completes the pending registrations from those reports in batches, at bounded concurrency, with hour-scale retries. Registration retry lives only in the registrar, so a struggling catalog sees one gentle attempt per job rather than a retry storm, and an outage produces a registration backlog costing zero compute instead of thousands of failed jobs.
- When the upload path itself fails, the output goes to a BNL interim stash: the BNL production door backing BNL_PROD_DISK_1 — the endpoint jobs already write logs to — registered in the BNL Rucio instance, a separate server from the JLab catalog and therefore reachable when JLab Rucio is the failing component. The interim entry is explicitly marked as staging until the ops agent registrar moves data and registration to the JLab catalog of record. The stash's plan of record is [RUCIO_FAILOVER_STASH.md](RUCIO_FAILOVER_STASH.md).

BNL storage operations are asked to confirm the allocation behind BNL_PROD_DISK_1 supports science-scale temporary overflow. Site-side buffering is the last option when no wide-area path works.

These run script changes land in the epicprod payload, the production team's run script cloned into this tree and evolved here (EPICPROD_PAYLOAD.md); the September run-script changes planned by the production team land there too.
