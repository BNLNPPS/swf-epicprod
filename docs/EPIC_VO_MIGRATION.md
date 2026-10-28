# epic VO cutover

Torre approved new submissions under `epic` on 25 October 2026, with
legacy `wlcg` work included in the same regulator allowance. Existing
tasks retain their identities. The cutover was deployed and accepted on
28 October under his renewed instruction to finish it. The recovered
draft is retained for provenance; the implementation is published on main.

## Order

1. Publish and install the shared-accounting regulator before changing
   submission defaults. It sums equivalent work queues from both VOs,
   uses one grant ledger scope, and keeps production and test separate.
   Missing peer queues or failed statistics reads hold generation.
2. Copy the current production pool policy to canonical `epic`. Keep
   the active OSG queue separate from the retired draining queue. The
   queued floor remains 3,123 and MODE remains throttle.
3. On pandaserver01, preserve the installed swf-epicprod package and
   configuration before installing the published commit with
   `/opt/panda/bin/pip install --no-deps --force-reinstall`. Follow the
   package procedure in distcomp-services/panda-server/UPGRADE.md.
   This changes the ePIC plugin package, not panda-server source.
4. Stop JEDI generation while installing the plugin and wait until the
   old grant ledger's 180-second reservation lifetime has elapsed before
   starting JEDI. The existing PanDA HTTP service and running payloads
   continue. Retain the old ledger as evidence; do not empty it.
5. Require fresh `REGULATION_OK` lines from both VOs with
   `accounting=epic+wlcg` and the same logical scope for equivalent
   queues before enabling new submission defaults.
6. Publish and deploy the submission builders, set production templates
   to `epic` through the authenticated, audited configuration route,
   then verify a managed `epic` job through output registration.

The deployment and readbacks are recorded in
distcomp-services/panda-server/CHANGES.md. The task is tracked as
`swf-epic-vo-migration` in TJAI. Shared regulator accounting does not
establish a cause for a loss of OSG slots: runnable-job and pilot supply
must be checked independently.

## Accepted on 28 October

[Task 41495](https://epic-devcloud.org/prod/panda/tasks/41495/) is `done`.
Its one [job, 6339107](https://epic-devcloud.org/prod/panda/jobs/6339107/),
finished with VO `epic`, label `managed`, 100 events and no reported
errors. It ran on BNL_OSG_PanDA_1 from 20:35:02 to 20:43:14 UTC under
the configured operator account `claude-ec2dev`. JEDI generated it with
FQAN `/epic/EIC/Role=production` after the shared OSG buffer drained
below its floor; dispatch followed generation within one second.

The trial's FULL and RECO datasets each contain one 100-event file,
with an `AVAILABLE` replica at BNL-XRD:

| Level | Bytes | Adler-32 |
|---|---:|---|
| FULL | 674526 | 3ac996bd |
| RECO | 2638652 | 7c4b16c9 |

Both are under
`epic:/TEST/trial/group.EIC.26.07.3.epic_craterlake.p1108.e37.s11.r11.130to177deg.trial2/`.
The ordinary PanDA log file is `ready` (79,343 bytes). This is a bounded
validation sample, not additional campaign production credit.

The focused checks passed: 28 throttler, four regulation-evidence and five
submission/permission checks. A permission-test mistake changed 19 live
templates before the regulator cutover. Generation was stopped, the shared
regulator installed and old grants allowed to expire before resuming.
The approved `epic` values were retained; the original audit records remain
unchanged. [Corrective audit 7152516](https://epic-devcloud.org/prod/logs/7152516/)
identifies the executor and synthetic test identity. Those tests now forbid
database access, and the migration action also checks administrator access
inside its body.
