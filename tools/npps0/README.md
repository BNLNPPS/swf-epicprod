# npps0 — standalone PanDA pilot for BNL_NPPS_GPU

The NPPS GPU server (2× NVIDIA RTX 4090) runs PanDA jobs for the
Simphony GPU work through the `BNL_NPPS_GPU` queue, in the minimal
single-host arrangement: the standard BNL pilot wrapper from CVMFS,
run in pull mode under a launcher loop. No Harvester and no compute
element. The host sits outside the SCDF network perimeter, which
makes it the working model for volunteer-class workers
(`docs/VOLUNTEER_GPU_PLAN.md`).

## Pieces

- `epicprod-gpu-pilot-launcher.sh` — the loop the systemd service
  runs. Each pilot pass is a child serialized by a per-GPU flock and
  reaped by `timeout(1)`. `KillMode=process` means a service stop or
  restart kills only the loop: an in-flight pass survives and
  completes, and the next launcher instance waits on the lock.
- `epicprod-gpu-pilot.sh` — one pilot pass: environment, workdir
  rotation, configuration seeding, then the CVMFS wrapper
  (`/cvmfs/eic.opensciencegrid.org/panda/bnlpanda.runpilot2-wrapper.sh`)
  against `BNL_NPPS_GPU`. Read fresh at every pass, so a deployed
  change takes effect at the next pass boundary with no service
  action.
- `epicprod-gpu-pilot.service` — systemd unit running the launcher.
  Installed at `/etc/systemd/system/`.
- `config/queuedata.json` — the queue's pilot-side behavior,
  git-sourced. The pass script copies it into the run directory,
  where the wrapper prefers it (`file://$PWD/queuedata.json`) over
  the CRIC-derived cache. CRIC retains only the queue's existence;
  every behavior field lives here under version control. Current
  deltas from the CRIC copy: log stage-out (`pl`) uses the `s3`
  copytool and the `DEV_CLOUD_S3` storage element.
- `config/agis_ddmendpoints.json` — the storage catalog: a snapshot
  of the CRIC ddmendpoints set plus `DEV_CLOUD_S3` (an `OS_LOGS`
  object store on the devcloud S3 bucket; see
  `docs/DEVCLOUD_STAGEOUT.md`). The pass script seeds it into the run
  directory under the pilot info system's LOCAL and USER-cache
  filenames, with `PILOT_HOME` anchoring the pilot's cache directory
  there.

Scripts install at `~wenaus/bin/`, configuration at
`~wenaus/npps0-config/` on npps0.

## Host prerequisites (in place 2026-08-14)

- CVMFS with the BNL squid (`cvmfs-cache.sdcc.bnl.gov:3128`), cache
  sized to 15 GB because the root volume is 27 GB;
  `eic.opensciencegrid.org` probes OK.
- apptainer; queue `container_options` carries `--nv` for GPU
  visibility. Container images must be CVMFS unpacked directories
  (`/cvmfs/singularity.opensciencegrid.org/eicweb/...`); the pilot's
  container layer does not accept local SIF files.
- boto3 for the system python3 (pilot s3 copytool dependency).
- Credentials (mode 600):
  - PanDA OIDC token at `~/.pathena/.token`.
  - Rucio proxy at `~/creds/longproxy-for-rucio`, a copy of the
    pandaserver02 production proxy (source `/etc/swf-monitor/`); the
    npps0 copy must be refreshed when the source renews. Used by the
    `rucio` copytool activities that still point at lab storage.
  - JLab EVGEN proxy at `~/creds/eicprod-proxy-for-jlab`.
  - AWS profile `epic-stageout` in `~/.aws/credentials` for S3 log
    stage-out, selected by `PANDA_PILOT_AWS_PROFILE`.
- Disk: no scratch volume; `/home` carries everything. The pilot
  workdir budget matches the queue's `maxwdir` (30 GB per slot);
  apptainer cache and tmp are pointed at `/home`
  (`APPTAINER_CACHEDIR`, `APPTAINER_TMPDIR`) because the root volume
  is small.

## Network position

npps0 cannot reach SCDF-internal hosts: the dCache doors
(`dcintdoor.sdcc.bnl.gov`, root:1094 and davs:443) and other
perimeter-internal services are blocked from its subnet. Stage-out
therefore goes to the devcloud S3 bucket; lab RSEs are unreachable
from this host by design of the surrounding network, not by choice
of configuration.

## Deploying a change

Copy the changed files to the host; the next pass boundary picks
them up. No service action is needed for script or configuration
changes:

    # from tools/npps0/ (two-hop route; see access notes)
    scripts  -> ~wenaus/bin/
    config/* -> ~wenaus/npps0-config/

Unit-file changes require `systemctl daemon-reload`; the new unit
takes effect at the next natural service cycle. Never kill a pass
that holds a job: check the queue's active jobs first (the launcher
makes restarts safe for the loop, but process-level surgery on a
pass is still unprotected).
