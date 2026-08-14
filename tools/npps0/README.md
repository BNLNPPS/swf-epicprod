# npps0 — standalone PanDA pilot for BNL_NPPS_GPU

The NPPS GPU server (2× NVIDIA RTX 4090) runs PanDA jobs for the
Simphony GPU work through the `BNL_NPPS_GPU` queue, in the minimal
single-host arrangement: the standard BNL pilot wrapper from CVMFS,
run directly on the node under systemd, in pull mode. No Harvester and
no compute element — the provisioning layer for one always-on box is
`Restart=always` (design record: `docs/JEDI_INTEGRATION.md`).

## Pieces

- `epicprod-gpu-pilot.sh` — one pilot pass: environment, workdir
  rotation, then the CVMFS wrapper
  (`/cvmfs/eic.opensciencegrid.org/panda/bnlpanda.runpilot2-wrapper.sh`)
  against `BNL_NPPS_GPU`. Installed at `~wenaus/bin/` on npps0.
- `epicprod-gpu-pilot.service` — systemd unit; one pilot pass per
  cycle, `RestartSec=300`. Installed at `/etc/systemd/system/`.

## Host prerequisites (in place 2026-08-14)

- CVMFS with the BNL squid (`cvmfs-cache.sdcc.bnl.gov:3128`);
  `eic.opensciencegrid.org` probes OK.
- apptainer; queue `container_options` carries `--nv` for GPU
  visibility.
- Credentials (mode 600 copies of the pandaserver02 production set):
  PanDA OIDC token at `~/.pathena/.token`, Rucio proxy at
  `~/creds/longproxy-for-rucio`, JLab EVGEN proxy at
  `~/creds/eicprod-proxy-for-jlab`. The Rucio proxy is the short one —
  it renews on pandaserver02 (source `/etc/swf-monitor/`) and the
  npps0 copy must be refreshed with it.
- Disk: no scratch volume; `/home` carries everything. The pilot
  workdir budget matches the queue's `maxwdir` (30 GB per slot);
  apptainer cache and tmp are pointed at `/home`
  (`APPTAINER_CACHEDIR`, `APPTAINER_TMPDIR`) because the root volume
  is small.

## Deploying a change

Edit here, copy to the host, restart the unit:

    scp epicprod-gpu-pilot.sh npps0:bin/ && ssh npps0 sudo systemctl restart epicprod-gpu-pilot
