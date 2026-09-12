# NERSC Perlmutter: how pilots start, and the test queue

Perlmutter runs ePIC production through a harvester operated by PanDA
operations on the site's login node. This document records how a pilot
comes to run on a Perlmutter node, what of that path is reachable from
the production system, and the arrangement under which production
operations controls the pilot on its own Perlmutter test queue. The
Event Service design that the test queue serves is
[NODE_EVENT_DISPATCHER.md](NODE_EVENT_DISPATCHER.md); the proving
program that precedes Perlmutter is
[NPPS0_TEST_QUEUE.md](NPPS0_TEST_QUEUE.md).

## The queues

| Queue | Role | Record |
|---|---|---|
| `NERSC_Perlmutter_epic` | production | `jobseed` `std`; mcore; `maxtime` 14400 s |
| `NERSC_Perlmutter_epic_es` | production operations' test queue for the Event Service | created 2026-09-10 as a clone of `NERSC_Perlmutter_epic_dev`; `jobseed` `all`; mcore; `apptainer:pilot` with `/global`, `/cvmfs` and `/pscratch` bound; rucio copytool to `BNL_PROD_DISK_1`; `maxtime` 345600 s; ordinary jobs run on it |

`jobseed` `all` is the field that lets the server create a new consumer
for a task's unprocessed ranges (NODE_EVENT_DISPATCHER.md, The pilot
side); the production queue keeps `std` until the worker-shape change.

## How a pilot starts

Read from the worker record of a probe job on 2026-09-12 (job 2723589,
worker 20654, Slurm 58243510). The harvester submits a Slurm job in the
`m3763` allocation on one 256-core node. The job script copies the
wrapper `wrapper-wrapper-3-epic-test.sh` from the harvester
installation (`/global/common/software/m3763/panda-harvester/etc/panda/`)
into the worker directory on `/pscratch` and runs it under
`srun -n 80 <wrapper> <queue> <accessPoint>`: eighty single-core pilots
per node, each with `HARVESTER_ID`, `HARVESTER_WORKER_ID` and `GTAG` in
its environment. Each task sets up ALRB from `/cvmfs/atlas.cern.ch`
(`lsetup python pilot-default-SL9`, rucio, xrootd, davix, psutil,
logstash, prmon), writes an environment file and a payload script, and
starts an apptainer AlmaLinux 9 container from the ATLAS container
repository with `/global`, `/pscratch` and `/cvmfs` bound. Inside it the
pilot runs as

```
python3 <harvester installation>/pilot/pilot3-<version>/pilot3/pilot.py \
  -q <queue> -i PR -j managed -w generic \
  --url https://pandaserver01.sdcc.bnl.gov -p 25443 \
  --queuedata-url http://pandaserver01.sdcc.bnl.gov:25080/cache/schedconfig/<queue>.all.json \
  --pilot-user epic --allow-same-user=False --job-type=managed \
  --use-rucio-traces False --rucio-host https://nprucio01.sdcc.bnl.gov:443 \
  --getjobrequests=50 --cleanup True --noworkerpilotstatusupdate --debug
```

The pilot is an unpacked release directory in the harvester
installation, and the version is a variable in the wrapper (3.14.3.3 on
2026-09-12). CVMFS plays no part in delivering it. The wrapper, the
Slurm template and the harvester queue configuration are not in a
repository that production operations can find; a change to any of
them is a request to PanDA operations with the change spelled out,
and the request below asks that they be kept in one.

## The worker record is public

`https://portal.nersc.gov/cfs/m3763/panda/jobs/<queue>/<pandaid>/`
serves, without login, the pilot log, the Slurm job's stdout and the
task's stdout and stderr; the task stdout carries the rendered wrapper,
the environment, the container command and the pilot command line
before the pilot's own log. The job record's `pilotid` names the
directory, and `panda_study_job` returns these files as `log_urls`.
Everything in the section above is read from there.

## CVMFS on the nodes

The canary fingerprint, taken on every probe landing, reports
`eic.opensciencegrid.org` unreachable on Perlmutter nodes and
`singularity.opensciencegrid.org` reachable; the worker record shows
`atlas.cern.ch` and `oasis.opensciencegrid.org` in use. The canary
pilot directory in `eic.opensciencegrid.org`
([OSG_SUBMISSION.md](OSG_SUBMISSION.md) § Our canary pilot directory)
therefore does not reach Perlmutter: a pilot for Perlmutter is
delivered by file or over HTTPS. Outbound HTTP and HTTPS from the
compute nodes work; the pilot's own traffic to the PanDA server and to
Rucio proves it.

## Pilot-side queue configuration without a CRIC edit

The ePIC pilot user module reads queue data in the order LOCAL, PANDA,
CVMFS, CRIC (`pilot/user/epic/setup.py`): a `queuedata.json` in the
pilot's working directory is used first and without an age check
(`pilot/info/dataloader.py`); `--queuedata-url` is the PANDA source.
A file placed in the working directory before the pilot starts
therefore governs every pilot-side field, as it does on `BNL_NPPS_GPU`
and in the OSG submit description. The fields the server reads
(`jobseed`, `corecount`, `capability`, `resource_type`, `maxtime`,
`status`) stay in CRIC.

For the Event Service the file must declare the `es_events` and
`es_failover` activities, in `acopytools` as `rucio` and in `astorages`
as `BNL_PROD_DISK_1`, the route the queue's ordinary outputs take.
Undeclared, the pilot's event service stage-out forces the
`objectstore` copytool (`pilot/api/es_data.py`) and fails against a
storage that is not an object store.

## Controlling the pilot on the test queue

Production operations owns the pilot launch that runs on
`NERSC_Perlmutter_epic_es`: the script that prepares what a pilot needs
on the node and starts it. It lives in this repository at
`perlmutter/NERSC_Perlmutter_epic_es/epicprod-perlmutter-pilot-launch.sh`,
named to travel alone (a cached copy in the site's tree, a copy in
every worker directory): whose it is, where it runs, what it does. It
is served from `main`, as everything in this repository is: `main` is what
runs. It is written against the interface the site's Slurm job
provides (the two arguments, the three environment variables, one
task per pilot under `srun`), not copied from the site's wrapper, and
it carries what the test queue needs: the pilot from a tarball
production operations builds and serves from the public `pilot/`
prefix of the devcloud bucket ([DEVCLOUD_STAGEOUT.md](DEVCLOUD_STAGEOUT.md)
§ 4; every place a pilot of ours goes, and how each is fed, is the
table in [OSG_SUBMISSION.md](OSG_SUBMISSION.md) § Our canary pilot
directory), the `queuedata.json` above
placed in the pilot's working directory, `PILOT_ES_EXECUTOR_TYPE` and
the channel library for the Event Service executor, and the pilot
options.

The site's Slurm job takes it by queue name, keeping a cached copy in
the harvester installation, where the job script now copies the site's
own wrapper:

```
OURS=/global/common/software/m3763/panda-harvester/etc/panda/queues/${PQ}/epicprod-perlmutter-pilot-launch.sh
mkdir -p "$(dirname "$OURS")"
curl -sfL "https://raw.githubusercontent.com/BNLNPPS/swf-epicprod/main/perlmutter/${PQ}/epicprod-perlmutter-pilot-launch.sh" -o "$OURS.new" && mv -f "$OURS.new" "$OURS"
cp "$OURS" epicprod-perlmutter-pilot-launch.sh 2>/dev/null || cp /global/common/software/m3763/panda-harvester/etc/panda/wrapper-wrapper-3-epic-test.sh epicprod-perlmutter-pilot-launch.sh
```

and the `srun` line runs `./epicprod-perlmutter-pilot-launch.sh` in place of
the site's file name.

The scope is set by the URL: a queue has a cached copy only if a file
is published under its name, so only `NERSC_Perlmutter_epic_es` runs
production operations' wrapper; every other queue's fetch fails and
the site's wrapper runs as before. On the test queue a successful fetch
replaces the cached copy atomically; a failed fetch leaves it, and the
worker runs the last good version; the site's wrapper is reached only
before the first successful fetch, and a one-time seed of the cached
copy removes even that. Once a queue has a cached copy, removing the
file from the repository does not return the queue to the site's
wrapper; every change in either direction is a publication on `main`.

The request to PanDA operations is these four lines in the Slurm job
script, the seed, and that the Perlmutter harvester configuration,
Slurm template and wrapper be kept in a repository, so that a change
like this one is a pull request.

## Canary probes at the site

A landing probe on Perlmutter is a whole-node Slurm allocation: the
harvester brings up a worker for it, and the worker ends a few minutes
later (job 2723589: a 256-core node for five minutes, for a
1.2-minute probe). The probe interval is a property of the queue on the
canary probes page; for `NERSC_Perlmutter_epic` it is 12 hours
(2026-09-12). Between probes the queue's health reading comes from the
production jobs on it, which a fed queue always has.

## Related

- [NODE_EVENT_DISPATCHER.md](NODE_EVENT_DISPATCHER.md): the design the
  test queue serves; the pilot side and the round trip proved on
  `BNL_NPPS_GPU`.
- [NPPS0_TEST_QUEUE.md](NPPS0_TEST_QUEUE.md): the proving program, and
  what that queue cannot represent, which is the Perlmutter material
  here.
- [OSG_SUBMISSION.md](OSG_SUBMISSION.md): pilot selection and the
  canary pilot directory, which Perlmutter does not mount.
- site-canary `docs/PLAN.md`: the probes and the fingerprint.
