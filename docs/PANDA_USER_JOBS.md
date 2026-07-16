# PanDA User Jobs

Running user analysis jobs on the ePIC production resources: analysis
takes open slots up to a fair-share fraction, on a designated subset
of sites, while production occupies the remainder. The enabling
mechanism is PanDA's global shares, which divides open compute slots
among activities by configurable fractions. This note records the
mechanism as verified in the panda-server code, the state of the BNL
EIC PanDA instance, and the configuration plan. The same mechanism can
later divide resources among further activities (distributed CI,
calibration) by adding leaves to the share tree.

## Mechanism

A share tree lives in the database table `global_shares` (schema
`doma_panda` on the Postgres-based BNL instance). Each row is a share:
a name, a value (the percentage of resources), a parent, and matching
fields (`prodsourcelabel`, `workinggroup`, `campaign`,
`processingtype`, `transpath`, `rtype`; regular expressions are
accepted). Every job is stamped with its matching share (`gshare`) at
insertion; jobs matching no share are stamped `Undefined` and sort
last at dispatch.

Dispatch is where the shares act. When a pilot requests a job, the
candidate query over activated jobs on that queue is ordered first by
share rank, with the share furthest under its target first, then by
`currentPriority`, then by age
(`pandaserver/taskbuffer/db_proxy_mods/entity_module.py`,
`getCriteriaForGlobalShares`). Share rank dominates priority: a
low-priority job of an under-target share is dispatched before a
high-priority job of an over-target share. A configurable fraction of
dispatches (`SLOPPY_DISPATCH_RATIO`, default 10%) is served
oldest-first, ignoring shares, as an anti-starvation valve; the
default stands.

The target for each share is its percentage of the total currently
executing HS06, recomputed from the `jobs_share_stats` aggregation
table on a 10-second cache; the share tree itself reloads on a 1-hour
cache, so share changes need no service intervention. The HS06
accounting multiplies job cores by the queue's `corePower`, so every
participating queue must carry a nonzero `corePower`.

The decisive condition is queue unification. The dispatcher's
candidate query filters by the label class the pilot requests:
production pilots are served only `managed`-family jobs, analysis
pilots only `user`-family jobs, and only pilots requesting the
`unified` label are served both classes in one candidate set
(`pandaserver/taskbuffer/db_proxy_mods/job_complex_module.py`,
`construct_where_clause`). Fair-share arbitration between production
and analysis for an open slot therefore happens only on queues
configured unified, with Harvester submitting grandly-unified
workers. On queues with separate production and analysis pilot
streams, shares reorder jobs only within each class.

## State of the BNL EIC Instance (verified 2026-07-15)

- The instance runs Postgres; panda-server translates its Oracle-form
  SQL at the cursor layer (`taskbuffer/WrappedCursor.py`), so the
  shares machinery works as deployed.
- `doma_panda.global_shares` exists and is empty: no share tree is
  defined, all jobs are stamped `Undefined`, and dispatch ordering
  reduces to plain priority.
- `doma_panda.jobs_share_stats` exists, is populated, and is fresh:
  the usage-aggregation machinery the targets depend on is already
  running.
- Of the 21 EIC queues, all are `type: production` except the two
  Perlmutter GPU queues, which are `type: unified`.
- `UM_GREX_PanDA_1` carries `corePower` 0.0, which would corrupt the
  HS06 targets; all other queues carry a nonzero value.
- The monitor's PanDA database account (`panda`) holds insert, update,
  and delete privilege on `doma_panda.global_shares`: the share tree
  is maintained from the production side, and the monitor's PanDA
  database browser provides inspection.

## Configuration Plan

1. **Share tree** (production side, via the monitor's database
   access). Set 2026-07-15, two top-level leaves: **Production 95**
   (`prodsourcelabel` regex `managed|test|prod_test|install`) and
   **Analysis 5** (`user|panda`), both `vo` `eic`, unthrottled. The
   tree and its values are production policy and this document records
   them; changes take effect within the 1-hour share reload. The
   Analysis page on the monitor shows the tree and the current usage
   by share stamp.
2. **Queue unification** (request to the PanDA service
   administrators). For the queues designated to carry analysis:
   `type: unified` in schedconfig and grandly-unified worker
   configuration in Harvester. The `UM_GREX_PanDA_1` `corePower` fix
   belongs to the same schedconfig request.
3. **Analysis site restriction** (ePIC submission convention). For a
   generic (non-ATLAS) VO, both production and analysis tasks are
   brokered by `GenJobBroker`; the ATLAS analysis-brokerage
   mechanisms, queue-type filtering and the
   `includedSite`/`excludedSite` task parameters, are not in effect.
   Two workable controls exist today: a `site` regular expression on
   the analysis task (honored by `GenJobBroker`, e.g.
   `SITE_A|SITE_B`), suitable when analysis submission goes through
   managed tooling; or a dedicated cloud partition containing exactly
   the analysis-approved queues, a hard fence maintained in
   schedconfig. The per-queue `catchall` processing-type gate also
   exists but matches exact client-version-dependent strings and is
   not recommended.
4. **Transient at turn-on.** `gshare` is stamped at job insertion, so
   jobs already activated when the tree is created remain `Undefined`
   and sort last until they drain, a self-correcting condition lasting
   one job generation.

## Queue Responsiveness

Job-start latency per queue, the quantity that decides where analysis
turnaround is acceptable, is measurable directly from PanDA accounting
data: the interval from job creation to job start in
`doma_panda.jobsarchived4`. Measured 2026-07-15 over the preceding 14
days (jobs reaching finished or failed; queues with more than 50
jobs):

| Queue | Jobs | Median wait | 90th percentile |
|---|---|---|---|
| BNL_OSG_PanDA_1 | 741 | 3.1 min | 4.4 min |
| BNL_OSG_PanDA_CI | 322 | 3.2 min | 6.7 min |
| NERSC_Perlmutter_epic_dev | 260 | 6.3 min | 8.3 min |
| NERSC_Perlmutter_epic_gpu_mps | 320 | 6.8 min | 8.6 min |
| NERSC_Perlmutter_epic_gpu_test | 68 | 7.6 min | 8.3 min |
| NERSC_Perlmutter_epic | 14224 | 18.8 min | 1.6 h |
| BNL_OSG_EPIC_PROD_1 | 24038 | 2.1 h | 8.8 h |
| BNL_PanDA_1 | 89 | 2.6 h | 2.6 h |
| UM_GREX_PanDA_1 | 14543 | 12.0 h | 28.3 h |
| NERSC_Perlmutter_epic_test | 27839 | 15.3 h | 16.3 h |

These are overwhelmingly production jobs, so the profile is the
no-shares baseline: the wait an analysis job would inherit by joining
the same activated backlog. Production-saturated queues wait hours;
lightly loaded short queues start in minutes. With global shares on a
unified queue, an under-share analysis job bypasses the activated
backlog and its wait reduces to slot turnover, so the fair-share
configuration above, not queue choice alone, is what delivers analysis
turnaround on the production queues. The per-queue profile remains the
instrument for choosing and watching the analysis-designated set, and
is cheap to compute on a cadence from accounting data as a monitor
metric.

## Upstream Note

The one genuine gap for generic-VO analysis steering is that
`GenJobBroker` does not read `includedSite`/`excludedSite`; support
would be a small change in the pattern of the existing single-`site`
regular-expression handling, and would give every generic VO clean
analysis-site control. ePIC is the motivating use case for proposing
it to the PanDA core team. The site-regex and cloud-partition controls
above do not depend on it.

## References

- Global shares concept: panda-docs `docs/source/advanced/gshare.rst`
- Share tree and targets: `pandaserver/taskbuffer/GlobalShares.py`;
  `pandaserver/taskbuffer/db_proxy_mods/entity_module.py`
  (`get_shares`, `__get_hs_leave_distribution`,
  `getCriteriaForGlobalShares`)
- Dispatch label classes:
  `pandaserver/taskbuffer/db_proxy_mods/job_complex_module.py`
  (`construct_where_clause`, `getJobs`)
- Generic brokerage: `pandajedi/jedibrokerage/GenJobBroker.py`;
  queue types in `pandaserver/taskbuffer/SiteSpec.py`
- Harvester unified workers:
  `pandaharvester/harvestercore/queue_config_mapper.py`
  (`get_source_label`)
