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

# Every stage runs under its own prmon, and its output is named for the
# stage. Three labels predate that convention and are named for the tool
# they monitored; any other label is reported under its own name, so a
# newly wrapped stage in run.sh needs no change here.
PRMON_LABEL_STAGES = {
    "hepmcmerger": "background",
    "npsim": "simulation",
    "eicrecon": "reconstruction",
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


def read_series(path):
    """The stage's prmon time series as (elapsed seconds, memory kB) pairs
    of its proportional set size, from the tab-separated file prmon writes
    a sample at a time. An absent or unreadable series yields nothing; the
    series is supporting detail, never a reason to fail."""
    points = []
    try:
        with open(path) as f:
            header = f.readline().split()
            if "Time" not in header or "pss" not in header:
                return points
            i_time, i_pss = header.index("Time"), header.index("pss")
            t0 = None
            for line in f:
                fields = line.split()
                if len(fields) <= max(i_time, i_pss):
                    continue
                try:
                    t, pss = int(fields[i_time]), float(fields[i_pss])
                except ValueError:
                    continue
                if t0 is None:
                    t0 = t
                points.append((t - t0, pss))
    except OSError:
        return []
    return points


def series_trend(points):
    """The memory trend of one stage: how many samples, over what span,
    the least-squares growth rate of its memory in kB per second, and how
    well a straight line describes it. A stage that grows steadily and a
    stage that plateaus are told apart by the rate and the fit together;
    the pilot computes the same shape for the job as a whole, and this is
    it per stage. Fewer than three samples yield the count alone."""
    n = len(points)
    if n < 3:
        return {"samples": n} if n else {}
    span = points[-1][0] - points[0][0]
    mean_t = sum(t for t, _ in points) / n
    mean_m = sum(m for _, m in points) / n
    var_t = sum((t - mean_t) ** 2 for t, _ in points)
    if var_t <= 0:
        return {"samples": n, "span_s": span}
    slope = sum((t - mean_t) * (m - mean_m) for t, m in points) / var_t
    intercept = mean_m - slope * mean_t
    ss_res = sum((m - (slope * t + intercept)) ** 2 for t, m in points)
    ss_tot = sum((m - mean_m) ** 2 for _, m in points)
    trend = {"samples": n, "span_s": span,
             "memory_growth_kb_per_s": round(slope, 2),
             "memory_start_kb": round(intercept),
             "memory_peak_kb": round(max(m for _, m in points))}
    if ss_tot > 0:
        trend["fit_quality"] = round(1 - ss_res / ss_tot, 3)
    return trend


def prmon_summary(prmon_dir, taskname):
    """Per stage, what prmon measured: the summary fields of PRMON_MAX,
    the CPU efficiency they imply, and the memory trend of the stage's
    time series. Stages are discovered from the prmon output present, so
    every wrapped stage reports; a summary that cannot be read is
    reported as an error under its own stage rather than dropped."""
    out = {}
    if not prmon_dir or not taskname:
        return out
    prefix, suffix = f"{taskname}.", ".prmon.json"
    try:
        names = sorted(os.listdir(prmon_dir))
    except OSError:
        return out
    for name in names:
        if not (name.startswith(prefix) and name.endswith(suffix)):
            continue
        label = name[len(prefix):-len(suffix)]
        stage = PRMON_LABEL_STAGES.get(label, label)
        path = os.path.join(prmon_dir, name)
        try:
            with open(path) as f:
                summary = json.load(f)
        except (OSError, ValueError) as e:
            out[stage] = {"error": f"{type(e).__name__}: {e}"}
            continue
        mx = summary.get("Max") or {}
        rec = {}
        for key, field in PRMON_MAX.items():
            if key in mx:
                rec[field] = mx[key]
        wall = rec.get("wall_s")
        cpu = (rec.get("cpu_user_s") or 0) + (rec.get("cpu_sys_s") or 0)
        if wall:
            rec["cpu_efficiency"] = round(cpu / wall, 3)
        version = (summary.get("prmon") or {}).get("Version")
        if version:
            rec["prmon_version"] = version
        trend = series_trend(read_series(path[:-len(suffix)] + ".prmon.txt"))
        if trend:
            rec["trend"] = trend
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
