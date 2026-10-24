#!/usr/bin/env python3
"""Select a pilot's admission floor by PanDA queue and exact GLIDEIN_Site.

The selected value is exported before either first-job or repeated-job
admission. This does not resize jobs or extend any of the pilot's clocks.
"""
import argparse
import json
import os
from pathlib import Path
import re
import sys


def positive(value):
    if type(value) is not int or value <= 0:
        raise ValueError('minimum_seconds must be a positive integer')
    return value


def execution_queue(machine_ad):
    if not machine_ad or not Path(machine_ad).is_file():
        return ''
    for line in Path(machine_ad).read_text().splitlines():
        match = re.match(r'^\s*GLIDEIN_Site\s*=\s*(.*?)\s*$', line)
        if match:
            value = json.loads(match[1])
            if not isinstance(value, str):
                raise ValueError('GLIDEIN_Site must be a string')
            return value
    return ''


def select(policy, queue, execute_queue, override=''):
    if policy.get('schema_version') != 1:
        raise ValueError('unsupported runtime policy schema')
    default = positive(policy['default_seconds'])
    queues = policy.get('queues', {})
    if not isinstance(queues, dict):
        raise ValueError('queues must be an object')
    # Validate every configured branch before accepting a staged policy.
    for item in queues.values():
        if not isinstance(item, dict):
            raise ValueError('queue policy must be an object')
        if 'minimum_seconds' in item:
            positive(item['minimum_seconds'])
        executions = item.get('execute_queues', {})
        if not isinstance(executions, dict):
            raise ValueError('execute_queues must be an object')
        for entry in executions.values():
            if not isinstance(entry, dict):
                raise ValueError('execution queue policy must be an object')
            positive(entry['minimum_seconds'])
    if re.fullmatch(r'[1-9][0-9]*', override):
        return int(override), 'environment override'
    entry = queues.get(queue, {})
    execution = entry.get('execute_queues', {}).get(execute_queue)
    if execution is not None:
        return execution['minimum_seconds'], 'execution queue'
    if 'minimum_seconds' in entry:
        return entry['minimum_seconds'], 'PanDA queue'
    return default, 'default'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--policy', required=True)
    parser.add_argument('--queue', default='')
    parser.add_argument('--machine-ad', default='')
    args = parser.parse_args()
    policy = json.loads(Path(args.policy).read_text())
    execute_queue = execution_queue(args.machine_ad)
    seconds, source = select(policy, args.queue, execute_queue,
                             os.environ.get('EPICPROD_MIN_TIME_FOR_NEW_JOB', ''))
    print(f'ePIC runtime policy: PanDA queue={args.queue or "unknown"}; '
          f'execution queue={execute_queue or "unknown"}; '
          f'minimum={seconds} s; source={source}', file=sys.stderr)
    print(seconds)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        sys.exit(f'ePIC runtime policy invalid: {exc}; no job taken')
