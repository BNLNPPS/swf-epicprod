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


def maximum(policy, queue, execute_queue):
    entry = policy.get('queues', {}).get(queue, {})
    value = entry.get('execute_queues', {}).get(execute_queue, {}).get(
        'maximum_seconds', entry.get('maximum_seconds'))
    if value is not None:
        if type(value) is not int or value <= 0:
            raise ValueError('maximum_seconds must be a positive integer')
        if value <= select(policy, queue, execute_queue)[0]:
            raise ValueError('maximum_seconds must exceed the admission minimum')
    return value


def prepare_queuedata(policy, queue, execute_queue, source, output):
    seconds = maximum(policy, queue, execute_queue)
    if seconds is None:
        return ''
    if '://' in source:
        raise ValueError('schedconfig must be transferred as a local file; worker URL fetch disabled')
    record = json.loads(Path(source).read_text())
    if record.get('panda_queue', record.get('nickname')) != queue:
        raise ValueError('queue data does not belong to ' + queue)
    record['maxtime'] = seconds
    output = Path(output).resolve()
    temporary = output.with_suffix('.tmp')
    temporary.write_text(json.dumps(record, indent=4) + '\n')
    temporary.replace(output)
    print(f'ePIC runtime maximum: queue={queue}; execution queue={execute_queue or "unknown"}; '
          f'maxtime={seconds} s; source={source}', file=sys.stderr)
    return str(output)


def condor_admission(policy, queue):
    """Match only slots with the same headroom the landing check requires."""
    expression = str(select(policy, queue, '')[0])
    for site in sorted(policy.get('queues', {}).get(queue, {}).get('execute_queues', {})):
        seconds = select(policy, queue, site)[0]
        expression = ('ifThenElse(TARGET.GLIDEIN_Site =?= ' + json.dumps(site)
                      + ', ' + str(seconds) + ', ' + expression + ')')
    return ('(TARGET.GLIDEIN_ToDie =?= undefined || '
            'TARGET.GLIDEIN_ToDie - time() > ' + expression + ')')


def condor_maximum(policy, queue):
    """Minutes, using the execution-site attribute recorded at match time."""
    default = maximum(policy, queue, '')
    if default is None:
        return None
    expression = str((default + 59) // 60)
    for site, entry in sorted(policy['queues'][queue].get('execute_queues', {}).items()):
        if 'maximum_seconds' in entry:
            seconds = maximum(policy, queue, site)
            expression = ('ifThenElse(MachineAttrGLIDEIN_Site0 =?= ' + json.dumps(site)
                          + ', ' + str((seconds + 59) // 60) + ', ' + expression + ')')
    return expression


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--policy', required=True)
    parser.add_argument('--queue', default='')
    parser.add_argument('--machine-ad', default='')
    parser.add_argument('--prepare-queuedata')
    parser.add_argument('--output')
    args = parser.parse_args()
    policy = json.loads(Path(args.policy).read_text())
    execute_queue = execution_queue(args.machine_ad)
    if args.prepare_queuedata is not None:
        if not args.output:
            parser.error('--output is required with --prepare-queuedata')
        print(prepare_queuedata(policy, args.queue, execute_queue,
                               args.prepare_queuedata, args.output))
        return
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
