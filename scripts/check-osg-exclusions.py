#!/usr/bin/env python3
"""check-osg-exclusions.py — does the submit host match what we display.

The OSG exclusions are operative in a submit description on the submit
host and are rendered in the monitor from `swf_epicprod.osg_exclusions`.
That file cannot be read from the monitor host on a schedule — ssh to
the submit host is refused except through the facility gateway with
agent forwarding — so the two can drift. This compares them and says so.

Run from a session that can reach the submit host::

    python scripts/check-osg-exclusions.py            # compare
    python scripts/check-osg-exclusions.py --print    # show what we expect

Exit 0 when they agree, 1 when they differ, 2 when the file could not be
read. A difference is reported in full; nothing is written or repaired
here, because the submit description is production configuration on a
shared service host.
"""
import argparse
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from swf_epicprod import osg_exclusions as x  # noqa: E402

GATEWAY = 'wenauseic@ssh.sdcc.bnl.gov'
HOST = 'osgsub01'


def deployed_lines():
    """The Requirements and UNDESIRED_Sites lines as deployed, or None."""
    inner = (f"ssh -o BatchMode=yes -o StrictHostKeyChecking=no {HOST} "
             f"'grep -E \"^Requirements|^\\+UNDESIRED_Sites\" {x.SUBMIT_FILE}'")
    try:
        out = subprocess.run(
            ['ssh', '-A', '-o', 'BatchMode=yes',
             '-o', 'StrictHostKeyChecking=accept-new', GATEWAY, inner],
            capture_output=True, text=True, timeout=90)
    except subprocess.SubprocessError as e:
        print(f'could not reach {HOST}: {e}', file=sys.stderr)
        return None
    if out.returncode != 0:
        print(f'could not read {x.SUBMIT_FILE}: {out.stderr.strip()}',
              file=sys.stderr)
        return None
    return out.stdout


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--print', dest='show', action='store_true',
                    help='print the expected clause and site list, no ssh')
    args = ap.parse_args()

    expected_clause = x.requirements_clause()
    expected_sites = [s['site'] for s in x.EXCLUDED_SITES]
    if args.show:
        print('Requirements clause:\n' + expected_clause)
        print('\nUNDESIRED_Sites: ' + ', '.join(expected_sites))
        return 0

    text = deployed_lines()
    if text is None:
        return 2

    problems = []
    if expected_clause not in text:
        problems.append('the node-exclusion clause on the submit host does '
                        'not match the rendered list')
    for site in expected_sites:
        if site not in text:
            problems.append(f'site {site} is displayed as excluded but is '
                            f'not in the deployed +UNDESIRED_Sites')

    print('deployed:')
    for line in text.strip().splitlines():
        print('  ' + line.strip())
    if problems:
        print('\nDRIFT:')
        for p in problems:
            print('  - ' + p)
        print('\nexpected clause:\n  ' + expected_clause)
        return 1
    print(f'\nin agreement: {len(x.EXCLUDED_SITE_NODES)} node(s), '
          f'{len(expected_sites)} site(s)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
