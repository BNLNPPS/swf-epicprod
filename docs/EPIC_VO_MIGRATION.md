# epic VO cutover

Torre approved new submissions under `epic` on 25 October 2026, with
legacy `wlcg` work included in the same regulator allowance. Existing
tasks retain their identities. The recovered draft is being completed
under his renewed instruction on 28 October.

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
