#!/usr/bin/env python3
"""Interim contract executable: one synrad_service process per work unit.

Consumes work units from the shared directory per
docs/WORK_UNIT_CONTRACT.md, running the existing one-shot synrad_service
binary for each — geometry reloads every unit, so this pays the startup
cost the resident loop removes. It exists to exercise the contract and the
dispatcher/agent plumbing with real transport until the resident-loop
service (the Windows-track deliverable) replaces it behind the same
contract. Linux-only: runs the service inside the eic_dev_cuda container.

Supports both spec forms: "generator" (type gun -> -n/-s/-I/-f) and
"input" (path -> -i). OPTICKS_MAX_SLOT is sized from the photon count
(gun count, or the input .npy's first dimension).

Usage:
  exec_oneshot.py --work WORKDIR --prefix SIMPHONY_PREFIX
                  --geom GEOM_NAME --geom-cfbase DIR --geom-edition NAME
                  [--container PATH] [--device 0] [--unit-timeout 600]
                  [--once]
"""
import argparse
import json
import platform
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path

CONTAINER = "/cvmfs/singularity.opensciencegrid.org/eicweb/eic_dev_cuda:nightly"

def ts():
    return time.strftime("%Y-%m-%dT%H:%M:%S")

def log(msg):
    print(f"{ts()} {msg}", flush=True)


def npy_rows(path):
    """First dimension of a .npy array, from the header alone."""
    with open(path, "rb") as f:
        magic = f.read(6)
        if magic != b"\x93NUMPY":
            raise ValueError(f"{path}: not a .npy file")
        major, _minor = f.read(1)[0], f.read(1)[0]
        hlen = struct.unpack("<H" if major == 1 else "<I",
                             f.read(2 if major == 1 else 4))[0]
        header = f.read(hlen).decode("latin1")
    shape = header.split("'shape':", 1)[1].split("(", 1)[1].split(")", 1)[0]
    return int(shape.split(",")[0])


def device_info(device):
    """GPU name and driver via nvidia-smi; blank on any failure, visibly."""
    try:
        out = subprocess.run(
            ["nvidia-smi", f"--id={device}",
             "--query-gpu=name,driver_version", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=30)
        if out.returncode == 0:
            name, driver = [s.strip() for s in out.stdout.strip().split(",", 1)]
            return name, driver
        log(f"WARNING nvidia-smi failed: {out.stderr.strip()}")
    except (OSError, subprocess.TimeoutExpired, ValueError) as e:
        log(f"WARNING nvidia-smi unavailable: {e}")
    return "", ""


def parse_summary(stdout):
    """counts and timing from the synrad-service summary line."""
    for line in stdout.splitlines():
        if line.startswith("synrad-service:"):
            tok = line.split()
            g = lambda key: int(tok[tok.index(key) + 1])
            return ({"generated": g("photons"), "wall_absorbed": g("wall-absorbed"),
                     "reflected": g("reflected>=1"), "on_caps": g("on-caps")},
                    {"transport_s": float(tok[tok.index("transport") + 1]),
                     "us_per_photon": float(tok[tok.index("=") + 1])})
    raise ValueError("no synrad-service summary line in output")


class Exec:

    def __init__(self, args):
        self.args = args
        # absolute: the inner script cds into the unit dir, so any relative
        # path here silently redirects the service outputs
        args.work = args.work.resolve()
        args.geom_cfbase = str(Path(args.geom_cfbase).resolve())
        self.inbox = args.work / "inbox"
        self.outbox = args.work / "outbox"
        for d in (self.inbox, self.outbox):
            d.mkdir(parents=True, exist_ok=True)
        self.gpu_name, self.gpu_driver = device_info(args.device)
        self.units_done = 0

    def service_cmd(self, spec, outdir):
        """(argv tail for synrad_service, photon count) from the unit spec."""
        if "generator" in spec:
            gen = spec["generator"]
            if gen.get("type") != "gun":
                raise ValueError(f"unsupported generator type: {gen.get('type')}")
            p = gen["params"]
            beam = ",".join(str(v) for v in
                            (*p["pos"], *p["dir"], p["emin_kev"], p["emax_kev"]))
            return (["-n", str(gen["count"]), "-s", str(gen["seed"]),
                     "-I", beam, "-f", str(p["fan_mrad"]), "-o", str(outdir)],
                    gen["count"])
        if "input" in spec:
            path = Path(spec["input"]["path"])
            if not path.is_file():
                raise ValueError(f"input photons not found: {path}")
            return ["-i", str(path), "-o", str(outdir)], npy_rows(path)
        raise ValueError("spec has neither generator nor input")

    def run_unit(self, spec_path):
        t_start = time.time()
        spec = json.loads(spec_path.read_text())
        uid = spec["unit_id"]
        tmpdir = self.outbox / f".{uid}.tmp"
        if tmpdir.exists():
            shutil.rmtree(tmpdir)
        tmpdir.mkdir(parents=True)
        record = {"contract_version": 1, "unit_id": uid, "status": "error",
                  "geometry_edition": self.args.geom_edition,
                  "generator": spec.get("generator", spec.get("input")),
                  "device": {"name": self.gpu_name, "driver": self.gpu_driver,
                             "platform": platform.platform()},
                  "process": {"units_since_init": 1, "rss_mb": 0, "vram_mb": 0},
                  "failures": []}
        try:
            if spec.get("contract_version") != 1:
                raise ValueError(f"contract_version {spec.get('contract_version')} != 1")
            if spec.get("geometry_edition") != self.args.geom_edition:
                raise ValueError(
                    f"geometry_edition {spec.get('geometry_edition')!r} != "
                    f"resident {self.args.geom_edition!r}")
            tail, count = self.service_cmd(spec, tmpdir)
            a = self.args
            inner = (
                f"export LD_LIBRARY_PATH='{a.prefix}/lib:{a.prefix}/lib64'"
                f"${{LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}}\n"
                # an inherited assignment (e.g. the PanDA job's GPU) wins
                f"export CUDA_VISIBLE_DEVICES=${{CUDA_VISIBLE_DEVICES:-{a.device}}}\n"
                f"export GEOM={a.geom}\n"
                f"export {a.geom}_CFBaseFromGEOM='{a.geom_cfbase}'\n"
                f"export OPTICKS_MAX_SLOT={count + 100000}\n"
                f"cd '{tmpdir}'\n"
                f"'{a.prefix}/bin/synrad_service' " +
                " ".join(f"'{t}'" for t in tail) + "\n")
            if a.exec_mode == "container":
                cmd = ["apptainer", "exec", "--nv", a.container, "bash", "-c", inner]
            else:               # direct: already inside the container
                cmd = ["bash", "-c", inner]
            (tmpdir / "service.sh").write_text(inner)
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=a.unit_timeout)
            (tmpdir / "service.log").write_text(
                proc.stdout + ("\n==== stderr ====\n" + proc.stderr
                               if proc.stderr else ""))
            if proc.returncode != 0:
                raise RuntimeError(
                    f"synrad_service exit {proc.returncode}: "
                    f"{proc.stderr.strip()[-500:]}")
            counts, timing = parse_summary(proc.stdout)
            hits = tmpdir / "synrad_service_hits.npy"
            if not hits.is_file():
                raise RuntimeError("no synrad_service_hits.npy produced")
            hits.rename(tmpdir / "hits.npy")
            record.update(status="ok", counts=counts)
            record["timing"] = {**timing, "generate_s": 0.0, "launches": 1,
                                "stage_wait_s": 0.0,
                                "unit_wall_s": round(time.time() - t_start, 3)}
        except (ValueError, RuntimeError, OSError,
                subprocess.TimeoutExpired, json.JSONDecodeError) as e:
            record["failures"].append({"stage": "unit", "message": str(e)})
            log(f"ERROR {uid}: {e}")

        # publish: rename tmpdir into place, marker file last
        udir = self.outbox / uid
        if udir.exists():
            shutil.rmtree(udir)
        if record["status"] == "ok":
            (tmpdir / "unit.json").write_text(json.dumps(record, indent=1))
            tmpdir.rename(udir)
            (udir / "done").touch()
        else:
            (tmpdir / "error.json").write_text(json.dumps(record, indent=1))
            tmpdir.rename(udir)
        spec_path.unlink()
        self.units_done += 1
        log(f"unit {uid}: {record['status']} "
            f"({record.get('counts', {}).get('generated', 0)} photons)")

    def run(self):
        log(f"exec_oneshot on {self.gpu_name or 'unknown GPU'}, "
            f"edition {self.args.geom_edition}, work {self.args.work}")
        while True:
            specs = sorted(self.inbox.glob("*.unit.json"))
            if specs:
                self.run_unit(specs[0])
                continue
            if self.args.once:
                log(f"inbox empty, {self.units_done} unit(s) processed; exiting (--once)")
                return
            time.sleep(1.0)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--work", required=True, type=Path)
    ap.add_argument("--prefix", required=True, help="simphony install prefix")
    ap.add_argument("--geom", required=True, help="GEOM envvar value")
    ap.add_argument("--geom-cfbase", required=True,
                    help="directory containing CSGFoundry/")
    ap.add_argument("--geom-edition", required=True,
                    help="edition string checked against unit specs")
    ap.add_argument("--container", default=CONTAINER)
    ap.add_argument("--exec-mode", choices=("container", "direct"), default="container",
                    help="direct: run the binary without apptainer (already in-container)")
    ap.add_argument("--device", type=int, default=0)
    ap.add_argument("--unit-timeout", type=float, default=600.0)
    ap.add_argument("--once", action="store_true",
                    help="exit when the inbox is empty")
    args = ap.parse_args()
    Exec(args).run()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
