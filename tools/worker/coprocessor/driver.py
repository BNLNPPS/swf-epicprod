#!/usr/bin/env python3
"""Coprocessor workflow driver — the PanDA job payload.

Runs one batch of work units through the coprocessor chain and judges the
result. In self-contained mode (the loopback form, and the PanDA payload
on BNL_NPPS_GPU) it spawns the dispatcher, worker agent, and interim
executable itself, so the whole chain lives and dies with the job and its
accounting; against a remote dispatcher (--dispatcher) it only enqueues,
waits, and collects.

The batch is K gun units (zero input, per docs/WORK_UNIT_CONTRACT.md),
optionally preceded by a reference-set unit whose counts are checked
against the expected values — the physics canary inside the job. Outputs
written to --outdir: units/<id>.json per-unit records and job_summary.json;
hit arrays are verified on the dispatcher side and fetched only with
--keep-hits. Exit 0 only if every unit succeeded and the reference counts
matched.

Typical PanDA payload:
  git clone --depth 1 https://github.com/BNLNPPS/swf-epicprod.git &&
  python3 swf-epicprod/tools/worker/coprocessor/driver.py \
      --units 3 --count 100000 --exec-mode direct \
      --prefix ~/work/simphony-synrad-install \
      --geom synrad --geom-cfbase ~/work/synrad-refset-20260820-release/geometry \
      --geom-edition synrad_bench-v1 \
      --refset-input ~/work/synrad-refset-20260820-release/inphoton/synrad_service_inphoton.npy
"""
import argparse
import json
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REFSET_EXPECT = {"wall_absorbed": 500000, "reflected": 362156, "on_caps": 11316}
BEAM = {"pos": [0.0, 0.0, 100.0], "dir": [0.0, 0.007, 1.0],
        "emin_kev": 0.3, "emax_kev": 19.4, "fan_mrad": 0.0}

def ts():
    return time.strftime("%Y-%m-%dT%H:%M:%S")

def log(msg):
    print(f"{ts()} driver: {msg}", flush=True)


def free_port():
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def http_json(method, url, obj=None, timeout=60):
    data = json.dumps(obj).encode() if obj is not None else None
    with urllib.request.urlopen(
            urllib.request.Request(url, data=data, method=method), timeout=timeout) as r:
        return json.loads(r.read())


class Chain:
    """The self-contained dispatcher + exec + agent, owned by the driver."""

    def __init__(self, args, workdir):
        self.procs = []
        self.logs = []
        port = free_port()
        self.base = f"http://localhost:{port}"
        self._spawn("dispatcher", [
            sys.executable, str(HERE / "dispatcher.py"), "--port", str(port),
            "--state", str(workdir / "dispatcher.db"),
            "--results", str(workdir / "results")], workdir)
        self._spawn("exec", [
            sys.executable, str(HERE / "exec_oneshot.py"),
            "--work", str(workdir / "work"), "--prefix", args.prefix,
            "--geom", args.geom, "--geom-cfbase", args.geom_cfbase,
            "--geom-edition", args.geom_edition,
            "--exec-mode", args.exec_mode, "--container", args.container,
            "--unit-timeout", str(args.unit_timeout)], workdir)
        self._spawn("agent", [
            sys.executable, str(HERE / "worker_agent.py"),
            "--dispatcher", self.base, "--work", str(workdir / "work"),
            "--worker", "driver-local"], workdir)

    def _spawn(self, name, cmd, workdir):
        logf = open(workdir / f"{name}.log", "w")
        self.logs.append(logf)
        self.procs.append((name, subprocess.Popen(cmd, stdout=logf, stderr=logf)))
        log(f"spawned {name} pid {self.procs[-1][1].pid}")

    def check(self):
        for name, p in self.procs:
            rc = p.poll()
            if rc is not None:
                raise RuntimeError(f"{name} exited early with code {rc} (see {name}.log)")

    def shutdown(self):
        for name, p in self.procs:
            if p.poll() is None:
                p.terminate()
        for name, p in self.procs:
            try:
                p.wait(timeout=15)
            except subprocess.TimeoutExpired:
                log(f"WARNING {name} did not terminate; killing")
                p.kill()
        for f in self.logs:
            f.close()


def build_units(args):
    units = []
    if args.refset_input:
        units.append({
            "contract_version": 1, "unit_id": f"{args.task_name}-refset",
            "geometry_edition": args.geom_edition,
            "input": {"path": args.refset_input}})
    for i in range(args.units):
        units.append({
            "contract_version": 1, "unit_id": f"{args.task_name}-{i:06d}",
            "geometry_edition": args.geom_edition,
            "generator": {"type": "gun", "version": 1, "count": args.count,
                          "seed": args.seed_base + i, "params": dict(BEAM)},
            "limits": {"max_photons_per_launch": 0}})
    return units


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--units", type=int, default=3)
    ap.add_argument("--count", type=int, default=100000)
    ap.add_argument("--seed-base", type=int, default=1000)
    ap.add_argument("--task-name", default="coproc")
    ap.add_argument("--refset-input", default="",
                    help="photon .npy making unit 0 the reference-set check")
    ap.add_argument("--prefix", required=True)
    ap.add_argument("--geom", required=True)
    ap.add_argument("--geom-cfbase", required=True)
    ap.add_argument("--geom-edition", required=True)
    ap.add_argument("--exec-mode", choices=("container", "direct"), default="container")
    ap.add_argument("--container",
                    default="/cvmfs/singularity.opensciencegrid.org/eicweb/eic_dev_cuda:nightly")
    ap.add_argument("--dispatcher", default="",
                    help="external dispatcher URL; default spawns the chain locally")
    ap.add_argument("--outdir", type=Path, default=Path("."))
    ap.add_argument("--workdir", type=Path, default=Path("coproc-run"))
    ap.add_argument("--unit-timeout", type=float, default=600.0)
    ap.add_argument("--deadline", type=float, default=3600.0)
    ap.add_argument("--keep-hits", action="store_true")
    args = ap.parse_args()

    args.workdir = args.workdir.resolve()
    args.outdir = args.outdir.resolve()
    args.workdir.mkdir(parents=True, exist_ok=True)
    units_dir = args.outdir / "units"
    units_dir.mkdir(parents=True, exist_ok=True)

    chain = None
    if args.dispatcher:
        base = args.dispatcher.rstrip("/")
    else:
        chain = Chain(args, args.workdir)
        base = chain.base

    failures = []
    records = {}
    try:
        # wait for the dispatcher to answer
        for _ in range(30):
            try:
                http_json("GET", base + "/status", timeout=5)
                break
            except (urllib.error.URLError, OSError):
                time.sleep(1)
        else:
            raise RuntimeError("dispatcher never answered /status")

        units = build_units(args)
        resp = http_json("POST", base + "/units", {"units": units})
        log(f"enqueued {resp['queued']} unit(s), duplicates {resp['duplicates']}")
        if resp["duplicates"]:
            raise RuntimeError(f"duplicate unit ids rejected: {resp['duplicates']}")

        total = len(units)
        t_end = time.time() + args.deadline
        while time.time() < t_end:
            if chain:
                chain.check()
            s = http_json("GET", base + "/status")
            states = s["states"]
            if states.get("done", 0) + states.get("error", 0) >= total:
                break
            time.sleep(5)
        else:
            raise RuntimeError(f"deadline: units incomplete after {args.deadline}s")

        for u in units:
            uid = u["unit_id"]
            rec = http_json("GET", f"{base}/unit/{uid}")
            records[uid] = rec
            (units_dir / f"{uid}.json").write_text(json.dumps(rec, indent=1))
            result = rec.get("result") or {}
            if rec["state"] != "done" or result.get("status") != "ok":
                failures.append(f"{uid}: state={rec['state']} "
                                f"failures={result.get('failures')}")
                continue
            if uid.endswith("-refset"):
                counts = result["counts"]
                bad = {k: (counts.get(k), v) for k, v in REFSET_EXPECT.items()
                       if counts.get(k) != v}
                if bad:
                    failures.append(f"{uid}: reference counts mismatch {bad}")
                else:
                    log(f"{uid}: reference counts reproduced exactly")
            if args.keep_hits:
                url = f"{base}/result/{uid}/hits.npy"
                with urllib.request.urlopen(url, timeout=120) as r:
                    (units_dir / f"{uid}.hits.npy").write_bytes(r.read())
    except (RuntimeError, urllib.error.URLError, OSError) as e:
        failures.append(str(e))
        log(f"ERROR {e}")
    finally:
        if chain:
            chain.shutdown()

    ok_units = [u for u, r in records.items()
                if r["state"] == "done" and (r.get("result") or {}).get("status") == "ok"]
    summary = {
        "task_name": args.task_name,
        "units_requested": args.units + (1 if args.refset_input else 0),
        "units_ok": len(ok_units),
        "photons": sum((r["result"]["counts"]["generated"]
                        for u, r in records.items() if u in ok_units), 0),
        "hits_bytes": sum(r.get("hits_bytes", 0) for r in records.values()),
        "us_per_photon": {u: r["result"]["timing"]["us_per_photon"]
                          for u, r in records.items() if u in ok_units},
        "failures": failures,
        "verdict": "PASS" if not failures else "FAIL",
    }
    (args.outdir / "job_summary.json").write_text(json.dumps(summary, indent=1))
    log(f"summary: {json.dumps(summary)}")
    print(f"COPROC-DRIVER: {summary['verdict']} "
          f"units {summary['units_ok']}/{summary['units_requested']} "
          f"photons {summary['photons']}", flush=True)
    return 0 if not failures else 1


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    sys.exit(main())
