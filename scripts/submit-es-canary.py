#!/usr/bin/env python3
"""Submit an Event Service payload canary as the operator
(docs/NERSC_PERLMUTTER.md, Payload canaries on the test queue).

Runs the deployed monitor's scripts/submit-evgen-task.py in the deployed
environment: production.env read as key/value data (it holds shell-sensitive
values and is never sourced), the operator's own DRF token (SWF_API_TOKEN in
~/.env) for the submission record, and the internal monitor URL, since the
external face refuses the record write. Arguments after ``--`` go to the
submitter unchanged; its output is printed and kept in --log.

    submit-es-canary.py --log <file> -- --task-name <ProdTask> --canary-stamp <stamp> \\
        --canary-queue NERSC_Perlmutter_epic_es --es-input-dataset <scope:name> ...
"""
import argparse
import os
from pathlib import Path
import subprocess
import sys

DEPLOYED = Path('/opt/swf-monitor/current')
PRODUCTION_ENV = Path('/opt/swf-monitor/config/env/production.env')
MONITOR_URL = 'https://pandaserver02.sdcc.bnl.gov/swf-monitor'


def key_values(path):
    values = {}
    for raw in Path(path).read_text().splitlines():
        raw = raw.strip().removeprefix('export ')
        if raw and not raw.startswith('#') and '=' in raw:
            key, value = raw.split('=', 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--log', type=Path, required=True, help='the submitter output, kept')
    ap.add_argument('--owner', default='wenaus', help='the ProdTask owner the record write names')
    ap.add_argument('--timeout', type=int, default=360)
    ap.add_argument('submitter_args', nargs=argparse.REMAINDER)
    args = ap.parse_args()
    passed = args.submitter_args[1:] if args.submitter_args[:1] == ['--'] else args.submitter_args
    if not passed:
        ap.error('give the submitter arguments after --')
    token = key_values(Path.home() / '.env').get('SWF_API_TOKEN')
    if not token:
        sys.exit('ERROR: SWF_API_TOKEN is not in ~/.env')
    env = dict(os.environ, **key_values(PRODUCTION_ENV))
    env.update(SWFMON_TOKEN=token, SWF_TASK_OWNER=args.owner, SWF_MONITOR_URL=MONITOR_URL)
    cmd = [str(DEPLOYED / '.venv/bin/python'), str(DEPLOYED / 'scripts/submit-evgen-task.py')] + passed
    args.log.parent.mkdir(parents=True, exist_ok=True)
    with args.log.open('w') as log:
        rc = subprocess.run(cmd, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=args.timeout).returncode
    print(args.log.read_text(), end='')
    print(f'submitter exit {rc}')
    return rc


if __name__ == '__main__':
    sys.exit(main())
