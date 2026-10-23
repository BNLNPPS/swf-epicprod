# Queue records on the PanDA server: CRIC with our overrides

The PanDA server on `pandaserver01.sdcc.bnl.gov` acts on its own copy of
each queue's record (schedconfig): which queues are push or pull, their
memory and time limits, `meanrss`, and the rest. It refreshes that copy
every 200 s from `CRIC_URL_SCHEDCONFIG` in `panda_server.cfg`. That
setting may name a local file as well as a URL (the configurator's
`aux.get_dump`), so the server reads a file we write: CRIC's records
with our settings on top. Every field the server acts on is then ours to
set, tracked here, without a CRIC edit. This is the server-side
counterpart of the pilot-side queue data
([OSG_SUBMISSION.md](OSG_SUBMISSION.md), Queue behavior without a CRIC
edit).

## The overrides

[`pandaserver/schedconfig-overrides.json`](../pandaserver/schedconfig-overrides.json)
maps a queue to the fields it sets, each as CRIC names it. A key
starting with `_` is a note kept with the setting and not applied. Every
override says why.

| Queue | Field | Value | Why |
|---|---|---|---|
| UM_GREX_PanDA_1 | `workflow` | `pull` | GREX runs pull pilots since 2026-10-19. Recorded as push, the queue fell under the server's meanRSS guard, which on a push queue whose running jobs average more memory than its `meanrss` (4000 MB) dispatches only jobs below it: on 2026-10-20 it held 10,125 eAu retries at 5,400 MB while GREX's 8-job slots ran the same jobs without a memory kill. |
| UM_GREX_PanDA_1 | `maxtime`, `timefloor` | 43200 seconds, 510 minutes | 22 October: the current job median is 68 minutes, rather than the 14-minute mix used to size the original five-hour allocation. The fixed submit request is now 12 hours; the deadline-aware launcher leaves at least three hours for the final job plus startup allowance. See OSG_SUBMISSION.md for rollout and measurement limits. |
| BNL_OSG_PanDA_1, BNL_OSG_EPIC_PROD_1 | `timefloor` | 120 | With 0 every pilot ran one job and left (994 jobs on 994 pilots in two hours at BNL_OSG_EPIC_PROD_1), so a queue ran only while a batch of its pilots stood at the front of the shared pool's line; a pilot now takes jobs for 120 minutes. The wrapper reads it from the server's cache, which these records feed (OSG_SUBMISSION.md, Queue behavior without a CRIC edit) (2026-10-21). |
| BNL_OSG_PanDA_1 | `corecount`, `maxrss` | 1, 4096 | The pilots are single-core and ask the pool for 3,700 MB (OSG_SUBMISSION.md); CRIC recorded 8 cores and 48,000 MB. The front's fit check and PCS readiness read the record, so the 5,400 MB eAu tasks 40856 and 40857 passed as fitting OSG and lost 55,824 jobs to memory kills on the two OSG queues in three days. 4096 is the default task declaration, which runs here. JEDI's job broker skips its memory check for a task whose site we set, so placed tasks are not affected (2026-10-21). |
| BNL_OSG_PanDA_1 | `maxtime` | 36000 seconds | Direct pilots reach slow OSPool slots (2026-10-23): at SU-ITS 41043 jobs took 269 to 339 minutes against about 128 elsewhere, and one was cut at 359 minutes by the 6-hour (21,600 s) limit. Ten hours lets a slow slot finish a job; a job on a fast slot ends in about two hours either way. The submit description's own runtime limit is raised with it (OSG_SUBMISSION.md). |
| BNL_OSG_PanDA_1, BNL_OSG_EPIC_PROD_1 | `workflow` | `pull` | Harvester on osgsub01 runs both queues' pilots in pull mode since 2026-10-19 (OSG_SUBMISSION.md); CRIC still recorded push, which left them under the server's meanRSS guard for push queues, the guard that starved GREX on 2026-10-20 (2026-10-21). |
| JLAB_PanDA_1 | the whole record | GREX's, with JLab's names, CE `osg-ce-2.jlab.org:9619`, `status` `brokeroff`, `corecount` 1, `maxrss` 6000, `maxtime` 86400 | The JLab farm through its own HTCondor-CE (account `pandas-eic`), pull pilots from pandaharvester01 as at GREX (2026-10-20). CRIC has no such queue. JLab's glidein slots in the OSG pool run only CLAS12 (their START requires `projectName` clas12), so this is ePIC's only route to the JLab farm. `brokeroff`: only jobs we place run there. |

A queue CRIC does not have is defined by `_clone`, the queue whose record
it copies; its other fields are set on the copy. The copy is taken after
that queue's own overrides, so the cloned queue comes earlier in the file
(JLAB_PanDA_1 is pull because GREX's override is). The server takes a
queue that is only in this file: its configurator loads every record of
the file into `schedconfig_json` and builds the queue's storage relation
from its `astorages`, and site lookups join `panda_site` without
requiring a row there.

## The writer

[`scripts/panda-schedconfig.py`](../scripts/panda-schedconfig.py) fetches
CRIC's records (verifying CRIC's certificate against the grid CA
directory), applies the overrides and replaces the output file
atomically. It changes nothing when CRIC cannot be read or when an
override names a queue or field CRIC does not have: the file keeps the
last good records, the server keeps reading them, and the run exits
nonzero. A CRIC outage therefore no longer reaches the server.

## On the server

- Installed from a commit of this repository into
  `/opt/panda/etc/panda/schedconfig/` (owner `atlpan`): the script, the
  overrides and the output, `schedconfig.json`. The commit installed is
  in `COMMIT` there.
- `/etc/cron.d/panda-schedconfig` runs the writer as `atlpan` every five
  minutes; its log is `/var/log/panda/panda-schedconfig-overrides.log`.
- `panda_server.cfg` sets `CRIC_URL_SCHEDCONFIG` to the output file; the
  CRIC URL is kept beside it as a comment. The configurator daemons read
  the setting when `panda_daemon` starts.

## On the submit host

Harvester reads the same records: its queue cache (`panda_queues.json`
in `[cacher]` of `panda_harvester.cfg`) supplies each queue's CEs to the
submitter and its fields to the worker maker, so a queue CRIC does not
have needs our file there too. Since 2026-10-20 on pandaharvester01:

- The script and overrides, from the same commit, in
  `/data/atlpan/harvester_common/schedconfig/` (owner `atlpan`, `COMMIT`
  names the commit); `/etc/cron.d/harvester-schedconfig` rewrites
  `schedconfig.json` there as `atlpan` every five minutes, log
  `schedconfig.log` beside it.
- `panda_harvester.cfg` reads `panda_queues.json` from
  `file:/data/atlpan/harvester_common/schedconfig/schedconfig.json`, the
  CRIC URL kept beside it as a comment (backup
  `panda_harvester.cfg-backup-2026-10-20`). Harvester reads the setting
  at start (`panda_harvester-uwsgi` restarted 20:50 UTC). Harvester
  therefore also sees GREX as pull, as the server does.
- JLAB_PanDA_1's harvester queue (`panda_queueconfig.json`, backups
  `-backup-2026-10-20` and `-20b`): GREX's entry with
  `submit_pilot3_pull_jlab.sdf`, 10 to 50 waiting workers, at most 200.
  The description is GREX's single-pilot one (backup
  `submit_pilot3_pull_umgrex.sdf-backup-2026-10-19b`) with one core,
  6,000 MB and `+maxWallTime` 1440 (minutes; JLab's Slurm holds a pilot
  without one), shipping `queuedata/JLAB_PanDA_1/queuedata.json`
  (`scripts/pilot-queuedata.py`, timefloor 180, maxtime 86400). The CE
  token is the one harvester's hourly `get-panda-tokens-for-ces.sh`
  already mints for `osg-ce-2.jlab.org:9619` into
  `/data/atlpan/tokens/ce/prod/`.

## Synchronization (22 October 2026)

All three consumers, pandaserver01, pandaharvester01 and osgsub01, run
`panda-schedconfig.py --sync-from-github` every five minutes. The writer
resolves this repository's `main` commit, fetches the overrides at that
immutable revision, validates and merges them with CRIC, and atomically
replaces the local output. GitHub or CRIC failure retains the previous
output and exits nonzero. A `.provenance.json` sidecar records the commit,
overrides SHA256, output SHA256 and write time. The hashes distinguish
content synchronization from a newer unrelated repository commit.

OSG Harvester reads `file:/var/data/atlpan/harvester_common/schedconfig/schedconfig.json`.
Its hot job statistics still come directly from PanDA and have their own
30-second refresh; this writer supplies static resource and endpoint data.
On pandaharvester01, `--pilot-dir` also refreshes the shipped GREX and JLab
queue files from the same merged records, retaining their timefloor policy.

GREX's job unit is one core, 6000 MB and at most twelve hours; its fixed submit
template separately requests eight cores and 48000 MB for eight concurrent
pilots. JLab explicitly keeps `timefloor` 180 instead of inheriting GREX's
longer intake. Both OSG queue records describe single-core pilots. Resource
requests and deployment measurements are recorded in
[the regulation implementation report](REGULATION_AUDIT_2026-10-22.md).
