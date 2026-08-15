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


def preprocess(spec: dict) -> None:
    """Write the in-container run script; runs in the job directory."""
    os.makedirs("workDir", exist_ok=True)
    with open(SPEC_FILE, "w") as f:
        json.dump(spec, f)
    lines = [
        "#!/bin/sh",
        "cd workDir || exit 64",
    ]
    for key, val in spec.get("env", {}).items():
        lines.append(f"export {key}={shlex.quote(str(val))}")
    lines += [
        "echo '=== epicrun payload start ==='",
        spec["run"],
        f"ec=$?; echo $ec > ../{STATUS_FILE}",
        "echo \"=== epicrun payload end (exit $ec) ===\"",
        "exit $ec",
    ]
    with open(RUN_SCRIPT, "w") as f:
        f.write("\n".join(lines) + "\n")
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
