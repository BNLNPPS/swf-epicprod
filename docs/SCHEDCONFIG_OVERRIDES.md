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

A change to an override is a commit here, an install of that commit on
the server, and an entry in the server's change log
(distcomp-services `panda-server/CHANGES.md`). The server takes it at
its next refresh, within 200 s of the writer's next run.
