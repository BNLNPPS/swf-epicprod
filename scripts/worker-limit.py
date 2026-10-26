#!/usr/bin/env python3
"""Apply or clear an operator worker limit and retain its control record.

Run in the monitor environment with the testbed venv and ~/.env.
Harvester reloads its queue configuration without a restart.
"""
import argparse
import datetime
import json
import os
from pathlib import Path
import re
import shlex
import subprocess


def remote(host, command, *, content=None):
    result = subprocess.run(
        ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', host,
         shlex.join(command)], input=content, text=True, capture_output=True,
        timeout=60, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip()
                           or f'Harvester command exited {result.returncode}')
    return result.stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('queue')
    parser.add_argument('limit', help='Positive worker count, or off to restore the prior limit')
    parser.add_argument('--host', required=True)
    parser.add_argument('--by', required=True)
    parser.add_argument('--reason', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', args.host):
        parser.error('invalid harvester host')
    clearing = args.limit.lower() == 'off'
    if not clearing and (not args.limit.isdigit() or int(args.limit) <= 0):
        parser.error('limit must be a positive worker count or off')

    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')
    import django
    django.setup()
    from django.db import transaction
    from monitor_app.epicprod_logging import log_epicprod_action
    from monitor_app.models import SysConfig
    from swf_epicprod.queues import regulated

    if args.queue not in regulated():
        parser.error('queue is not regulated by the production front')
    key = f'pilot.queue.{args.queue}.worker_limit'
    previous = SysConfig.get_config().get(key) or {}
    if clearing and not previous.get('active'):
        parser.error('queue has no active worker-limit override')
    if previous.get('active') and previous.get('host') != args.host:
        parser.error('active worker-limit override belongs to another harvester host')
    config_path = '/opt/harvester/etc/panda/panda_queueconfig.json'
    config = json.loads(remote(args.host, ['sudo', '-n', 'cat', config_path]))
    entry = config.get(args.queue)
    if not isinstance(entry, dict) or not isinstance(entry.get('maxWorkers'), int):
        raise RuntimeError('Harvester queue has no explicit integer maxWorkers')
    before = entry['maxWorkers']
    if previous.get('active') and before != previous.get('limit'):
        raise RuntimeError('Harvester maxWorkers changed outside the worker-limit control; '
                           f'recorded {previous.get("limit")}, observed {before}')
    baseline = previous['baseline'] if previous.get('active') else before
    limit = baseline if clearing else int(args.limit)
    print(json.dumps({'queue': args.queue, 'host': args.host, 'before': before,
                      'after': limit, 'active': not clearing, 'apply': args.apply}), flush=True)
    if not args.apply:
        return
    installer = Path(__file__).with_name('set-harvester-queue-limits.py').read_text()
    installed = remote(args.host, [
        'sudo', '-n', 'python3', '-', args.queue, f'maxWorkers={limit}',
        '--reason', args.reason, '--apply'], content=installer)
    print(installed, end='', flush=True)
    observed = json.loads(remote(args.host, ['sudo', '-n', 'cat', config_path]))
    if observed.get(args.queue, {}).get('maxWorkers') != limit:
        raise RuntimeError('Harvester maxWorkers did not retain the requested value')
    control = {'active': not clearing, 'limit': limit if not clearing else None,
               'baseline': baseline, 'host': args.host, 'by': args.by,
               'reason': args.reason,
               'at': datetime.datetime.now(datetime.timezone.utc).isoformat()}
    # Publish the control only after the Harvester file has been installed
    # and read back. Other SysConfig keys retain their current values.
    with transaction.atomic():
        SysConfig.update_config({key: control}, username=args.by)
        record = log_epicprod_action(
            'worker-control', 'worker_limit', subject_type='panda_queue',
            subject_key=args.queue, username=args.by, outcome='ok',
            sublevel='high', live_default=True,
            message=(f'{args.queue}: worker limit '
                     + (f'cleared; maxWorkers restored to {limit}' if clearing
                        else f'set to {limit}')),
            reason=args.reason, previous=before, updated=limit, control=control)
        if record is None:
            raise RuntimeError('Harvester limit changed, but its control audit failed')
    print(json.dumps({'control': control, 'audit': record}, default=str))


if __name__ == '__main__':
    main()
