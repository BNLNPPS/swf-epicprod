#!/usr/bin/env python3
"""Add only the Torre-approved MANITOBA-XRD -> EIC-XRD distance of one.

Default is a read-only plan. --apply makes one create request as eicprod,
with no JLab proxy fallback or permission changes. Existing edges are kept.
The private --state JSON records the result, including a permission denial.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path

from dotenv import dotenv_values
from rucio.common.exception import AccessDenied, CannotAuthenticate, Duplicate

from swf_epicprod.jlab_auth import rucio_client


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--state', type=Path, required=True)
    args = parser.parse_args()
    actor = os.environ['SWF_REMOTE_CLAUDE_USER']
    os.environ.update({k: v for k, v in dotenv_values(
        '/opt/swf-monitor/config/env/production.env').items() if v is not None})
    client = rucio_client(account='eicprod', proxy=None, timeout=30)
    if client.whoami().get('account') != 'eicprod':
        raise RuntimeError('Unexpected Rucio account')
    source, target = 'MANITOBA-XRD', 'EIC-XRD'
    result = {'at': datetime.now(timezone.utc).isoformat(), 'actor': actor,
              'account': 'eicprod', 'source': source, 'target': target,
              'requested_distance': 1, 'authorization':
              'Torre: do it, swf-teamcomms, 2026-10-28',
              'before': client.get_distance(source, target), 'outcome': 'read_only'}
    if args.apply:
        if result['before']:
            result['outcome'] = 'already_present'
        else:
            try:
                client.add_distance(source, target, {'distance': 1})
                result['outcome'] = 'created'
            except Duplicate:
                result['outcome'] = 'concurrently_created'
            except AccessDenied as error:
                result.update(outcome='access_denied', error=str(error))
            except CannotAuthenticate as error:
                result.update(outcome='authentication_failed', error=str(error))
        # A 401 can make the Rucio client attempt an interactive refresh;
        # make a fresh token client for this read without repeating the POST.
        reader = rucio_client(account='eicprod', proxy=None, timeout=30)
        result['after'] = reader.get_distance(source, target)
    args.state.parent.mkdir(parents=True, exist_ok=True)
    with args.state.open('w') as output:
        args.state.chmod(0o600)
        json.dump(result, output, indent=2, default=str)
        output.write('\n')
    print(json.dumps(result, default=str))
    return 1 if result['outcome'] in ('access_denied', 'authentication_failed') else 0


if __name__ == '__main__':
    raise SystemExit(main())
