# Manitoba to JLab replication test — 28 October 2026

Torre approved Sakib's bounded test of the existing trial's RECO and FULL
datasets, starting with RECO and proceeding to FULL only on success.
The operating owner was Codex swf-teamcomms on swf-testbed, configured
operator `claude-ec2dev`, Rucio account `eicprod` authenticated by token.

## Result

RECO rule `139a423c6e0248e3845f76a027308f5a` was created at 20:53:04 UTC,
copies one, target EIC-XRD, source expression MANITOBA-XRD, comment
`Manitoba->JLab replication test`, lifetime 86,400 seconds. It expires
29 October at 20:53:04 UTC.

At 20:56 UTC it was **STUCK**, with zero OK locks, zero replicating locks
and one stuck lock. The exact error was
`RequestErrMsg.NO_SOURCES:RequestState.NO_SOURCES`.
No current request remained; `eicprod` was denied request-history reads,
so no historical FTS ID or error was available. FTS authorization remains
untested. FULL was not started after the RECO-first failure.

The two requested DIDs are under
`epic:/TEST/trial/group.EIC.26.07.3.epic_craterlake.p1108.e37.s11.r11.130to177deg.trial/`:
RECO has one 2,638,877-byte file and FULL one 674,788-byte file. Both
source replicas were AVAILABLE at MANITOBA-XRD. The catalog's
`availability_read` and `availability_write` flags were true at both RSEs;
source HTTPS at port 8443 and destination DAVS/HTTPS at port 1094
advertised WAN third-party copy. The directed distance query returned
`[]`. That absence is a plausible source-selection blocker, not a
verified server-side diagnosis.

Sakib's originating session received the result. The single test rule was
left to expire normally; existing rules and RSE configuration were unchanged.

## Published operator procedure

`scripts/manitoba-replication-test.py` defaults to reading these exact two
DIDs, their rules, locks and transfer diagnostics. `--apply --level RECO`
creates at most one 24-hour target rule; `--apply --level FULL` requires an
OK RECO rule. Existing matching rules are reused. The private state file
records the requesting account, contents, rule IDs and pending POSTs;
an uncertain POST is never blindly repeated. Denied diagnostic reads are
recorded explicitly alongside the rule result.

Run with the deployed monitor Python and the production operator's
environment. Keep `--state` outside Git. The 28 October state is
`/data/wenauseic/pcs-operations/manitoba-replication-test-20261028.json`.
