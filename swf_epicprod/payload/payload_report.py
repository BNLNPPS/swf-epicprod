#!/usr/bin/env python3
"""Write the payload report, payload-report.json, from what a run of the
epicprod payload leaves behind: the stage log, the prmon summaries of the
background merge, simulation and reconstruction, the output files with
their event counts, and the registration outcome. run.sh calls it from
its EXIT trap, so a report is written on every exit path with whatever
stages were reached; the epicprod dispatcher carries it into
jobReport.json, which the pilot ships as job metadata (swf-epicprod
docs/EPICPROD_PAYLOAD.md, payload reporting).

Usage:
  payload_report.py --out FILE --exit RC --stages LOG --version FILE
      [--requested N] [--prmon-dir DIR --taskname NAME]
      [--full PATH] [--reco PATH] [--full-events N] [--reco-events N]
      [--note TEXT]

Exits 0 whatever happens; a report that cannot be written is said on
stderr, never raised into the payload's own exit.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

SCHEMA = "epicprod-payload-report/1"

# The prmon summary each stage writes, by the stage name in the stage log.
PRMON_STAGES = {
    "background": "hepmcmerger",
    "simulation": "npsim",
    "reconstruction": "eicrecon",
}

# prmon "Max" fields carried into the report (totals of monotonic
# counters, peaks of the others), with the report's name for each.
PRMON_MAX = {
    "wtime": "wall_s",
    "utime": "cpu_user_s",
    "stime": "cpu_sys_s",
    "rss": "rss_max_kb",
    "pss": "pss_max_kb",
    "vmem": "vmem_max_kb",
    "rchar": "rchar_bytes",
    "wchar": "wchar_bytes",
    "read_bytes": "read_bytes",
    "write_bytes": "write_bytes",
    "nprocs": "nprocs_max",
    "nthreads": "nthreads_max",
}


def _parse_time(text):
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def read_stage_log(path):
    """The stage log as [{at, stage, status, detail}], in order; empty
    when there is none."""
    entries = []
    try:
        with open(path) as f:
            for line in f:
                parts = line.rstrip("\n").split(" ", 3)
                if len(parts) >= 3:
                    entries.append({"at": parts[0], "stage": parts[1],
                                    "status": parts[2],
                                    "detail": parts[3] if len(parts) > 3 else ""})
    except OSError:
        pass
    return entries


def stage_summary(entries):
    """Per stage, in first-seen order: the final status, first start,
    last end, wall seconds between them, and the details recorded. A
    stage started and never ended is ``unfinished``."""
    out = {}
    for e in entries:
        s = out.setdefault(e["stage"], {"status": "unfinished", "started_at": None,
                                        "ended_at": None, "wall_s": None,
                                        "detail": []})
        if e["status"] == "start":
            if s["started_at"] is None:
                s["started_at"] = e["at"]
        else:
            s["ended_at"] = e["at"]
            s["status"] = e["status"]
            if e["detail"]:
                s["detail"].append(e["detail"])
    for s in out.values():
        if s["started_at"] and s["ended_at"]:
            try:
                s["wall_s"] = int((_parse_time(s["ended_at"])
                                   - _parse_time(s["started_at"])).total_seconds())
            except ValueError:
                pass
    return out


def prmon_summary(prmon_dir, taskname):
    """Per stage, the prmon summary fields of PRMON_MAX plus the CPU
    efficiency, from <prmon_dir>/<taskname>.<tool>.prmon.json; a stage
    whose summary is absent or unreadable is reported as such."""
    out = {}
    if not prmon_dir or not taskname:
        return out
    for stage, tool in PRMON_STAGES.items():
        path = os.path.join(prmon_dir, f"{taskname}.{tool}.prmon.json")
        if not os.path.exists(path):
            continue
        try:
            with open(path) as f:
                summary = json.load(f)
        except (OSError, ValueError) as e:
            out[stage] = {"error": f"{type(e).__name__}: {e}"}
            continue
        mx = summary.get("Max") or {}
        rec = {}
        for key, name in PRMON_MAX.items():
            if key in mx:
                rec[name] = mx[key]
        wall = rec.get("wall_s")
        cpu = (rec.get("cpu_user_s") or 0) + (rec.get("cpu_sys_s") or 0)
        if wall:
            rec["cpu_efficiency"] = round(cpu / wall, 3)
        version = (summary.get("prmon") or {}).get("Version")
        if version:
            rec["prmon_version"] = version
        out[stage] = rec
    return out


def output_record(path, events):
    """One output file: its name, size, and event count, each None when
    unknown; None when no path was given."""
    if not path:
        return None
    rec = {"file": os.path.basename(path), "bytes": None, "events": events}
    try:
        rec["bytes"] = os.path.getsize(path)
    except OSError:
        pass
    return rec


def registration_record(stages):
    """The registration outcome from the stage log: registered with its
    DIDs, failed with what failed, or not reached."""
    reg = stages.get("registration")
    if reg is None:
        return {"outcome": "not reached", "dids": [], "failed": []}
    dids = [d for d in reg["detail"] if d.startswith("/")]
    failed = [d for d in reg["detail"] if not d.startswith("/")]
    if reg["status"] == "ok":
        outcome = "registered"
    elif reg["status"] == "fail":
        outcome = "failed"
    else:
        outcome = reg["status"]
    return {"outcome": outcome, "dids": dids, "failed": failed}


def _int_or_none(text):
    text = (text or "").strip()
    return int(text) if text.isdigit() else None


def read_version(path):
    try:
        with open(path) as f:
            return f.readline().strip()
    except OSError:
        return ""


def build_report(args):
    entries = read_stage_log(args.stages)
    stages = stage_summary(entries)
    full_events = _int_or_none(args.full_events)
    reco_events = _int_or_none(args.reco_events)
    report = {
        "schema": SCHEMA,
        "payload_version": read_version(args.version),
        "exit_code": args.exit,
        "written_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "events": {
            "requested": _int_or_none(args.requested),
            "simulated": full_events,
            "reconstructed": reco_events,
        },
        "stages": stages,
        "prmon": prmon_summary(args.prmon_dir, args.taskname),
        "outputs": {
            "full": output_record(args.full, full_events),
            "reco": output_record(args.reco, reco_events),
        },
        "registration": registration_record(stages),
    }
    if args.note:
        report["note"] = args.note
    return report


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", required=True)
    ap.add_argument("--exit", type=int, required=True)
    ap.add_argument("--stages", required=True, help="the stage log")
    ap.add_argument("--version", required=True, help="the payload VERSION file")
    ap.add_argument("--requested", default="")
    ap.add_argument("--prmon-dir", default="")
    ap.add_argument("--taskname", default="")
    ap.add_argument("--full", default="")
    ap.add_argument("--reco", default="")
    ap.add_argument("--full-events", default="")
    ap.add_argument("--reco-events", default="")
    ap.add_argument("--note", default="")
    args = ap.parse_args()
    try:
        report = build_report(args)
        with open(args.out, "w") as f:
            json.dump(report, f)
        ev = report["events"]
        print(f"payload report written: {args.out} exit={args.exit} "
              f"events requested={ev['requested']} simulated={ev['simulated']} "
              f"reconstructed={ev['reconstructed']} "
              f"registration={report['registration']['outcome']}")
    except Exception as e:  # noqa: BLE001 - reported, never the payload's exit
        print(f"payload report not written: {type(e).__name__}: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
