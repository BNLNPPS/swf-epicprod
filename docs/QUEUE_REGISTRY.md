# Queue Registry

A production queue's status is one record. The front, the regulation
guards, the canaries, the NERSC balance, the monitor and the
computing-centre room all read it, and the copies that live outside the
monitor (harvester's queue configuration, the PanDA schedconfig
overrides, the JEDI throttler's pools) are checked against it every
cycle. Opening, closing, retiring or adding a queue is one change to the
record.

## Why

On 22 October 2026, retiring BNL_OSG_EPIC_PROD_1 took five separate fixes
between 07:12 and 00:53 the next day, because the queue's status lived in
five places: the front's queue list, the front's closed list, the
regulation module's retired list and capacity pools, the room's site list
and its not-in-production list. The two copies that were missed caused
the evening's OSG collapse: work landed on the retired queue at 15:02, and
the throttler read both queues as one pool from 16:51 to 18:16. Torre's
rule of one instance with full parity applies to state as it does to
code.

## The record

SysConfig `production.queues`, a list with one entry per queue, in the
front's order (a list, because the database does not keep the order of a
mapping's keys):

| Field | Meaning |
|---|---|
| `queue` | the PanDA queue name |
| `status` | one of the five below |
| `reason` | why the queue is not in production; shown wherever the status is |
| `since`, `by` | when and on whose word the status was set; empty for the entries adopted from the old settings |
| `site` | the room's site group the queue belongs to |
| `pool` | the capacity pool the queue shares slots with, if any (`osg`) |
| `front` | whether the pressure front regulates the queue |

| Status | Meaning | Closed to new work | In the room as |
|---|---|---|---|
| `production` | open to new work | no | PanDA's reading |
| `commissioning` | not a production queue yet; work placed by hand (trials, canaries) is accepted | no | off |
| `off` | closed for now, by an operator or by the NERSC balance | yes | off |
| `retired` | closed for good; the work already there drains in place | yes | left out |
| `not_built` | named, not yet usable | no | not built |

Closed to new work means the front never recommends the queue and PCS
refuses to place, submit, rerun, queue, trial or move work there
(`pcs.services.refuse_closed_queue`). Canary probes do not pass through
PCS submission; site-canary refuses only the retired queues. A retired
queue's tasks keep generating jobs, retrying and running, and its jobs
still count in its pool's capacity: closing admission is not a task
pause or a pilot shutdown.

The record is read and written through `swf_epicprod.queues`. Reads are
cached in each process for ten seconds.

## The readers

| Reader | What it reads | Replaces |
|---|---|---|
| The front (`front.regulated_queues`, `front.closed_queues`) and its page | the regulated queues, and every closed queue with its reason | SysConfig `front.queues`, `front.closed_queues` |
| Regulation (`regulation.RETIRED_QUEUES`, `CAPACITY_POOLS`) | the retired queues and the pools, as live views of the record; the names stay, so their importers (the census, worker admission, the job queries, the canary settings) are unchanged | the module's constants |
| The NERSC balance (`nersc_allocation.apply_queue_switches`) | sets a queue `off` with its `NERSC allocation:` reason, and back to `production` when the balance recovers | its writes to `front.closed_queues` |
| The room (swf-monitor `comp_snapshot`) | each site's queues and their status | the queue lists and statuses in SysConfig `comp.sites`, `NOT_IN_PRODUCTION` |

`comp.sites` keeps what is the room's own: the site groups' ids, names,
order and display flags.

## Adoption

Until the record is stored, the module derives it, read only, from the
settings it replaces: the front's two settings, the room's queue lists
and statuses, and the retirement and pool the regulation module held.
A queue closed in the front's list is `off`; a queue the room called off
but the front left open (JLAB_PanDA_1) is `commissioning`. Every reader's
answer is the one it gave before, so deploying the readers changes
nothing by itself.

Adoption stores the derived record, keeps the replaced settings under
SysConfig `production.queues.adopted_from` with the time and the
adopter, and removes them from the document. The first write to the
record adopts it; `scripts/queue-status.py --adopt --by NAME` adopts it
explicitly, and with `--strip-room` also drops the room's queue lists from
`comp.sites` once the deployed room reads its queues from the record.
To go back, restore the keys from `production.queues.adopted_from` on the
System page before deploying older code.

## Changing a queue's status

```bash
scripts/queue-status.py                      # the record
scripts/queue-status.py BNL_X off --reason 'site down' --by 'Torre Wenaus'
scripts/queue-status.py BNL_X production --by 'Torre Wenaus'
scripts/queue-status.py BNL_X retired --reason '...' --by '...'   # also leaves the front
scripts/queue-status.py BNL_NEW commissioning --reason '...' --by '...' --site jlab
```

Each change is a `queue_status` action. The record can also be edited as
JSON on the System page.

## The copies outside the monitor

Three systems cannot read the record and hold their own copy of part of
it:

| Copy | Host | Holds | Installer |
|---|---|---|---|
| harvester's `panda_queueconfig.json` | osgsub01, pandaharvester01 | `queueStatus` online or offline | `scripts/configure-osg-regulation.py` |
| PanDA's schedconfig overrides | pandaserver01 | the queue's PanDA status (`brokeroff` for PROD_1) | `pandaserver/schedconfig-overrides.json` |
| the JEDI throttler's configuration | pandaserver01, JEDI `config` table | `POOLS`: each pool's retired queues generate apart, in `<pool>_drain` (`queue-status.py --jedi-pools`) | `scripts/jedi-throttler-config.sh` |

## The drift check

A doer of the production operations agent, every cycle: it reads the
three copies and compares each with the record. A disagreement is a
finding: a `queue_drift` action naming the queue, the copy and both
values, posted to epicprod-live as a finding at its first appearance and
at each hour it persists, and cleared when the copy matches. It changes
nothing. It is the watcher's first item (EPICPROD_WATCHER.md).

## Status

Plan of record, 2026-10-23 (Torre Wenaus: "yes", the queue status as one
record that the front, the throttler, harvester's configuration, the
monitor and the room all read). Built in this order: the record and its
readers, then adoption once both repositories are deployed; the drift
check; the command's installation of the external copies.
