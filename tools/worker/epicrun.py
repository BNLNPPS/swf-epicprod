#!/usr/bin/env python3
"""epicrun — purpose-built worker executor for PanDA jobs (runGen replacement).

First draft (2026-08-14). Fills the transform slot in the pilot's
multiStepExec contract: the pilot runs `<trf> --preprocess <args>` on
the host, then the container command (which executes the run script
this preprocess writes), then `<trf> --postprocess <args>` on the
host. runGen is analysis-era scaffolding in that slot — URL-encoded
shell strings, client-side substitution devices, dataset-shaped
output plumbing. epicrun replaces it with a declared job spec.

The job spec is a base64-encoded JSON object passed in jobParameters
(base64 so it survives PanDA's parameter-string quoting without an
invented encoding):

    {
      "run": "shell command for the payload, executed in workDir",
      "outputs": {"<lfn>": "<path relative to workDir>", ...},
      "env": {"NAME": "value", ...}          # optional
    }

Submitter side: jobParameters = "--spec-b64 <blob>", the same spec's
outputs generate the job's FileSpecs, multiStepExec.containerOptions
runs `/bin/sh __run_main_exec.sh`, and the transform URL points at
this file (git-sourced, like the rest of the worker configuration).

Contract with the pilot, learned the measured way:
  - preprocess and postprocess run OUTSIDE the container, in the job
    directory; the payload runs INSIDE it, in workDir/.
  - the pilot stages out <job dir>/<lfn> for every declared output
    and builds the log tarball from the job directory.
  - the transform's exit code is the payload verdict; postprocess
    must propagate the payload's real status.
  - the pilot reads memory_monitor_output.txt and
    memory_monitor_summary.json from the job directory by name at every
    job update, whether or not it started the monitor itself, so the
    runner writes them there from the image's prmon.
"""

import argparse
import base64
import json
import os
import shlex
import sys

RUN_SCRIPT = "__run_main_exec.sh"
SPEC_FILE = ".epicrun_spec.json"
STATUS_FILE = ".epicrun_status"
# The job-level memory monitor's files, named as the pilot reads them
# from the job directory (pilot/user/epic/utilities.py,
# get_memory_monitor_info); its own log beside them.
PRMON_OUTPUT = "memory_monitor_output.txt"
PRMON_SUMMARY = "memory_monitor_summary.json"
PRMON_LOG = "memory_monitor.log"
PRMON_INTERVAL_S = 60

EC_OK = 0
EC_PAYLOAD = 1        # payload command failed (its status is in the report)
EC_MISSING_OUT = 65   # payload succeeded but a declared output is absent
EC_BADSPEC = 66       # spec undecodable


def load_spec(blob: str) -> dict:
    try:
        spec = json.loads(base64.b64decode(blob))
        assert isinstance(spec.get("run"), str) and spec["run"].strip()
        assert isinstance(spec.get("outputs", {}), dict)
        return spec
    except Exception as exc:
        print(f"epicrun: bad spec: {exc}", file=sys.stderr)
        sys.exit(EC_BADSPEC)


def run_script_lines(spec: dict) -> list:
    """The in-container run script, line by line. Pure, so the shape of
    what runs in the container is testable without a container.

    The payload runs under the image's prmon, watching this shell's
    process tree and writing the two files the pilot reads by name from
    the job directory — one level above the payload's working
    directory, which is where the runner already places declared
    outputs. The pilot's own job-level monitor never starts on ePIC
    jobs: its setup (`pilot/user/epic/utilities.py`,
    `get_memory_monitor_setup`) prefixes the command with
    `lsetup prmon;`, an ATLAS setup step these jobs do not have, so its
    launch fails and its memory accounting falls back to what the pilot
    sees from outside. It does not need to have launched the monitor to
    use its output: at every job update it reads
    `memory_monitor_output.txt` and `memory_monitor_summary.json` from
    the job directory and lifts the Max and Avg fields into the job
    record, and its over-memory path reads the same output. So the
    runner writes them (NPPS0_TEST_QUEUE.md, program step 6). Without
    prmon on the path the payload runs unwatched, as it does in run.sh.
    """
    lines = [
        "#!/bin/sh",
        "cd workDir || exit 64",
    ]
    for key, val in spec.get("env", {}).items():
        lines.append(f"export {key}={shlex.quote(str(val))}")
    lines += [
        "echo '=== epicrun payload start ==='",
        # The payload runs in a subshell of its own so prmon can watch
        # that process and end with it: prmon writes the summary when
        # the process it watches exits, and nothing else makes it write
        # one — measured in the campaign image, prmon 3.2.0, where a
        # SIGTERM leaves only the periodic
        # memory_monitor_summary.json_snapshot behind (exit 143). A
        # subshell also takes whatever the run string is, compound or
        # several lines, without the quoting an inline background job
        # would need.
        "(",
        spec["run"],
        ") &",
        "__payload=$!",
        "if command -v prmon >/dev/null 2>&1; then",
        f"  prmon --pid $__payload --filename ../{PRMON_OUTPUT} "
        f"--json-summary ../{PRMON_SUMMARY} "
        f"--interval ${{EPICRUN_PRMON_INTERVAL:-{PRMON_INTERVAL_S}}} "
        f"> ../{PRMON_LOG} 2>&1 &",
        "  __prmon=$!",
        "else",
        "  __prmon=''",
        "  echo 'epicrun: no prmon on the path; the job runs unwatched'",
        "fi",
        # The payload's status first: anything between it and this line
        # would be the status the job reports.
        "wait $__payload",
        "ec=$?",
        # prmon sees the payload end and writes its summary; give it
        # that moment rather than killing it, which would not.
        "if [ -n \"$__prmon\" ]; then",
        "  wait \"$__prmon\" 2>/dev/null",
        "fi",
        f"echo $ec > ../{STATUS_FILE}",
        "echo \"=== epicrun payload end (exit $ec) ===\"",
        "exit $ec",
    ]
    return lines


def preprocess(spec: dict) -> None:
    """Write the in-container run script; runs in the job directory."""
    os.makedirs("workDir", exist_ok=True)
    with open(SPEC_FILE, "w") as f:
        json.dump(spec, f)
    with open(RUN_SCRIPT, "w") as f:
        f.write("\n".join(run_script_lines(spec)) + "\n")
    os.chmod(RUN_SCRIPT, 0o755)
    print(f"epicrun: wrote {RUN_SCRIPT} for: {spec['run'][:200]}")


def postprocess(spec: dict) -> None:
    """Collect declared outputs into the job directory; propagate status."""
    try:
        with open(STATUS_FILE) as f:
            payload_ec = int(f.read().strip() or "1")
    except Exception:
        payload_ec = 1
        print("epicrun: no payload status file — treating as failed",
              file=sys.stderr)

    report = {"payload_exit_code": payload_ec, "outputs": {}}
    missing = []
    for lfn, rel in spec.get("outputs", {}).items():
        src = os.path.join("workDir", rel)
        if os.path.isfile(src):
            os.replace(src, lfn)
            report["outputs"][lfn] = os.path.getsize(lfn)
            print(f"epicrun: output {lfn} <- workDir/{rel} "
                  f"({report['outputs'][lfn]} bytes)")
        else:
            missing.append(lfn)
            print(f"epicrun: MISSING output workDir/{rel} (lfn {lfn})",
                  file=sys.stderr)
    report["missing"] = missing
    with open("epicrun_report.json", "w") as f:
        json.dump(report, f, indent=1)

    if payload_ec != 0:
        sys.exit(EC_PAYLOAD)
    if missing:
        sys.exit(EC_MISSING_OUT)
    sys.exit(EC_OK)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--preprocess", action="store_true")
    ap.add_argument("--postprocess", action="store_true")
    ap.add_argument("--spec-b64", required=True)
    args, _ = ap.parse_known_args()

    spec = load_spec(args.spec_b64)
    if args.preprocess:
        preprocess(spec)
    elif args.postprocess:
        postprocess(spec)
    else:
        # single-shot mode (local testing, or a pilot flow without
        # multiStepExec): write, run here, collect.
        preprocess(spec)
        ec = os.system(f"/bin/sh {RUN_SCRIPT}")
        _ = ec  # status lands in STATUS_FILE via the script itself
        postprocess(spec)


if __name__ == "__main__":
    main()
