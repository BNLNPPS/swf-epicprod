# Log stage-out fallback

A production job's physics output is registered by the payload itself, in
JLab Rucio. The only file PanDA stages out for an ePIC job is the pilot's log
tarball, written to a BNL RSE and registered in BNL Rucio. A failure of that
one transfer has failed jobs whose output was already registered: on
2026-10-01 and 10-02 thousands of jobs at UM_GREX_PanDA_1 and
BNL_OSG_EPIC_PROD_1 failed this way, while BNL Rucio returned HTTP 500 on its
authentication endpoint and while some pilots, lacking the queue's Rucio
configuration, authenticated against the ATLAS Rucio server and were refused.

The fallback keeps the log and the job. It has four parts.

## The pilot

When the log transfer fails, the ePIC pilot plugin
(`pilot/user/epic/common.py`, `log_stageout_fallback`; pilot3 fork branch
`epic-log-fallback`) asks the grant service for an upload grant and posts the
tarball to the devcloud stage-out bucket under
`logs/<site>/<log dataset>/<lfn>`. It uses the standard library only, since
the pilot runs on the ALRB Python, which carries no AWS library, and the
worker holds no credential. A log held this way does not fail the job: the
error codes of the failed transfer are removed, and the job metrics carry
`logHeld=s3`. The log is left out of the file information sent to the
server, which therefore records it as not transferred.

## The grant service

`GET /pcs/api/v1/stageout/log-grant/?pandaid=<id>&lfn=<lfn>`, reached from
workers as `https://epic-devcloud.org/prod/pcs/api/v1/stageout/log-grant/`
under the login wall's open `v1` prefix. The service
(`pcs.services.log_grant_request`) grants only for a job in `jobsactive4` in
a live state (starting, running, transferring, holding) whose log file is the
named one. The web tier holds no credential: the production operations agent
signs the grant (`log_grant` handler, `scripts/log-grant.py`) with the
stage-out profile and writes it to `$SWF_TMP_DIR/log-grants/<pandaid>.json`.
The service answers 202 with `retry_after` while the grant is signed, and 200
with the presigned POST once it exists. A grant names one object, caps the
size at 1 GiB, and lasts 12 hours.

## Recovery

Objects under `logs/` expire after 7 days (DEVCLOUD_STAGEOUT.md). Before
then, production operations copy each held log to the log dataset's RSE and
register it in BNL Rucio, once BNL Rucio takes it. A job failed only on its
log is credited from its payload report, which records the registered output.

## The adder

PanDA's adder fails a finished job whose log is missing from the pilot's
report (DDM 200), and it cannot register a log held in the object store
(NPPS0_TEST_QUEUE.md). Until the adder treats an unregistrable log as pending
rather than fatal, a job whose log is held still ends failed in PanDA; its
output and its log are kept, and the production record credits the output.
