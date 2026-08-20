#!/usr/bin/env python3
"""Contract executable wrapper: the resident work-unit loop of synrad_service.

Launches `synrad_service -w WORKDIR -g EDITION` (the loop of
tools/worker/synrad-service/work_unit_loop.h) with the environment
conventions of this directory: geometry resolution envvars, library path,
GPU selection with an inherited assignment winning, container or direct
execution. One process holds geometry and the OptiX context resident
across every unit — this replaces exec_oneshot.py wherever the loop-mode
service binary is installed.

Usage:
  exec_service.py --work WORKDIR --prefix SIMPHONY_PREFIX
                  --geom GEOM_NAME --geom-cfbase DIR --geom-edition NAME
                  [--container PATH] [--exec-mode container|direct]
                  [--device 0] [--max-units N]
"""
import argparse
import subprocess
import sys
import time
from pathlib import Path

CONTAINER = "/cvmfs/singularity.opensciencegrid.org/eicweb/eic_dev_cuda:nightly"

def ts():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--work", required=True, type=Path)
    ap.add_argument("--prefix", required=True)
    ap.add_argument("--geom", required=True)
    ap.add_argument("--geom-cfbase", required=True)
    ap.add_argument("--geom-edition", required=True)
    ap.add_argument("--container", default=CONTAINER)
    ap.add_argument("--exec-mode", choices=("container", "direct"), default="container")
    ap.add_argument("--device", type=int, default=0)
    ap.add_argument("--max-units", type=int, default=0,
                    help="exit after N units (0 = run until terminated)")
    args = ap.parse_args()

    work = args.work.resolve()
    (work / "inbox").mkdir(parents=True, exist_ok=True)
    (work / "outbox").mkdir(parents=True, exist_ok=True)
    cfbase = str(Path(args.geom_cfbase).resolve())

    inner = (
        f"export LD_LIBRARY_PATH='{args.prefix}/lib:{args.prefix}/lib64'"
        f"${{LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}}\n"
        f"export CUDA_VISIBLE_DEVICES=${{CUDA_VISIBLE_DEVICES:-{args.device}}}\n"
        f"export GEOM={args.geom}\n"
        f"export {args.geom}_CFBaseFromGEOM='{cfbase}'\n"
        f"cd '{work}'\n"
        f"exec '{args.prefix}/bin/synrad_service' -w '{work}' -g '{args.geom_edition}'"
        + (f" -N {args.max_units}" if args.max_units else "") + "\n")
    if args.exec_mode == "container":
        cmd = ["apptainer", "exec", "--nv", args.container, "bash", "-c", inner]
    else:
        cmd = ["bash", "-c", inner]

    print(f"{ts()} exec_service: resident loop, work {work}, "
          f"edition {args.geom_edition}", flush=True)
    proc = subprocess.run(cmd)
    print(f"{ts()} exec_service: service exited {proc.returncode}", flush=True)
    return proc.returncode


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
