#!/usr/bin/env python3
"""Rebalance existing idle OSG workers; Harvester remains the only refiller.

Python 3.6 on the submit host. No running worker is edited or removed.
"""
import argparse
from collections import Counter
from datetime import datetime, timedelta
import json
import math
import os
from pathlib import Path
import re
import subprocess
import time
import urllib.parse
import urllib.request


def targets(images, demand, running, budget, protected=None):
    """Waiting targets from runnable jobs and supply already running.

    Preserve protected canaries, bound each image by its queued job count,
    and give a runnable image at least one opportunity. Remaining capacity
    goes to deficits against the demand-weighted total pilot population.
    """
    if budget < 0:
        raise ValueError('negative waiting budget')
    protected = protected or {}
    out = {i: max(0, int(protected.get(i, 0))) for i in images}
    if sum(out.values()) > budget:
        raise ValueError('protected workers exceed waiting budget')
    q = {i: max(0, int(demand.get(i, 0))) for i in images}
    r = {i: max(0, int(running.get(i, 0))) for i in images}
    total_r = sum(r.values())
    total_need = total_r + sum(q.values())
    weights = {i: max(0, (total_r + budget) * (q[i] + r[i]) - r[i] * total_need)
               for i in images}
    for image in sorted(images, key=lambda i: (-q[i], i)):
        if q[image] and not out[image] and sum(out.values()) < budget:
            out[image] = 1
    # At most budget (600) unit decisions; deterministic and cheap.
    while sum(out.values()) < budget:
        eligible = [i for i in images if out[i] < q[i]]
        if not eligible:
            break
        image = max(eligible, key=lambda i: (weights[i] / float(out[i] + 1), q[i], i))
        out[image] += 1
    return out


def read_policy(path):
    value = json.loads(Path(path).read_text())
    if (value.get('schema_version') != 1 or value.get('queue') != 'BNL_OSG_PanDA_1'
            or not 1 <= int(value.get('waiting_limit', 0)) <= 600):
        raise ValueError('invalid OSG image policy')
    images = value.get('images')
    if (not isinstance(images, list) or not images or len(set(images)) != len(images)
            or any(not isinstance(i, str) or not i.startswith('/cvmfs/')
                   or any(c in i for c in '\n\r" {}') for i in images)):
        raise ValueError('invalid exact image paths')
    return value


def timestamp(value):
    return datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ')


def env_config(path):
    result = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        if line.startswith('export '):
            line = line[7:]
        key, value = line.split('=', 1)
        result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def command(args, **kwargs):
    return subprocess.check_output(args, universal_newlines=True, timeout=30, **kwargs)


def worker_image(ad, images):
    image = ad.get('EpicprodNativeImage') or ad.get('SingularityImage')
    if image not in images or ad.get('SingularityImage') != image:
        raise ValueError('worker image attributes disagree or are not approved')
    return image


def edit_image(ad, image, ready, base):
    """All coupled attributes change in one Condor queue transaction."""
    before = ad['Environment']
    old = ad.get('EpicprodNativeImage') or ad['SingularityImage']
    token = 'EPICPROD_NATIVE_IMAGE=' + old
    if before.split().count(token) != 1:
        raise ValueError('worker environment has no single exact image binding')
    env = before.replace(token, 'EPICPROD_NATIVE_IMAGE=' + image)
    job = str(ad['ClusterId']) + '.' + str(ad['ProcId'])
    cmd = ['sudo', '-n', 'condor_qedit', '-jobids', job, '-constraint',
           base + ' && JobStatus == 1',
           'Environment', json.dumps(env), 'SingularityImage', json.dumps(image),
           'EpicprodNativeImage', json.dumps(image),
           'EpicprodImageReady', 'True' if ready else 'False']
    done = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          universal_newlines=True, timeout=30)
    if done.returncode:
        # Condor also returns an error when the idle-only constraint stops
        # matching because this worker has just started or left the queue.
        # Confirm that race; a failed edit of a still-idle worker stays fatal.
        status = command(['condor_q', '-allusers', job, '-af', 'JobStatus']).strip()
        if not status or (status.isdigit() and int(status) != 1):
            return False
        raise RuntimeError('condor_qedit failed for ' + job + ': ' + done.stderr[:300])
    result = done.stdout
    if 'for 1 matching jobs' not in result:
        # A worker may start between observation and the transaction. Never
        # touch it after that; the next cycle observes the changed counts.
        return False
    return True


def run(args):
    import classad2
    policy = read_policy(args.policy)
    images = policy['images']
    config = env_config(args.env)
    template = Path(policy['template_file']).read_text()
    limits = {'cores': 1}  # This controller serves the queue's SCORE pilots only.
    for field, key in [('request_memory', 'memory_mb'), ('request_disk', 'disk_kb')]:
        match = re.search(r'(?m)^' + field + r'\s*=\s*([0-9]+)\s*$', template)
        if not match:
            raise ValueError('cannot establish pilot capacity from ' + field)
        limits[key] = int(match.group(1))
    url = (config['SWF_MONITOR_URL'].rstrip('/') + '/api/osg-image-demand/?'
           + urllib.parse.urlencode(limits))
    request = urllib.request.Request(url, headers={
        'Authorization': 'Token ' + config['SWF_REPORT_TOKEN']})
    began = time.monotonic()
    with urllib.request.urlopen(request, timeout=20) as reply:
        demand_record = json.load(reply)
    fetched_ms = round(1000 * (time.monotonic() - began), 1)
    age = (datetime.utcnow() - timestamp(demand_record['observed_at'])).total_seconds()
    if (demand_record.get('queue') != policy['queue'] or age < -60
            or age > policy['demand_max_age_seconds']):
        raise ValueError('runnable image demand is missing or stale')
    demand = {row['image']: int(row['activated']) for row in demand_record['images']}
    base = ('Owner == "atlpan" && harvesterID == "BNL_osg_harvester_1" && '
            'EpicprodNativeMode == "direct" && '
            'regexp("(^| )-q BNL_OSG_PanDA_1( |$)", Args)')
    raw = command(['condor_q', '-allusers', '-constraint', base + ' && (JobStatus == 1 || JobStatus == 2)',
                   '-long', '-attributes', 'ClusterId,ProcId,QDate,JobStatus,Environment,Args,'
                   'SingularityImage,EpicprodNativeImage,EpicprodImageReady,EpicprodImageTrial'])
    ads = list(classad2.parseAds(raw))
    waiting = sorted((a for a in ads if a['JobStatus'] == 1),
                     key=lambda a: (a['QDate'], a['ClusterId'], a['ProcId']))
    running = Counter(worker_image(a, images) for a in ads if a['JobStatus'] == 2)
    protected = Counter(worker_image(a, images) for a in waiting if a.get('EpicprodImageTrial'))
    # A schedd restart can return previously running workers to idle. Keep
    # the matching budget bounded and park the surplus instead of refusing
    # every subsequent allocation cycle while the queue is above the cap.
    budget = min(len(waiting), policy['waiting_limit'])
    desired = targets(images, demand, running, budget, protected)
    remaining = {i: max(0, desired[i] - protected[i]) for i in images}
    edits = []
    movable = [a for a in waiting if not a.get('EpicprodImageTrial')]
    assignments = {}
    # Keep current assignments where possible, avoiding image churn.
    for ad in reversed(movable):
        image = worker_image(ad, images)
        if remaining[image] > 0:
            assignments[(ad['ClusterId'], ad['ProcId'])] = (image, True)
            remaining[image] -= 1
    for ad in movable:
        key = (ad['ClusterId'], ad['ProcId'])
        if key in assignments:
            continue
        image = next((i for i in images if remaining[i] > 0), None)
        if image:
            remaining[image] -= 1
            assignments[key] = (image, True)
        else:
            assignments[key] = (worker_image(ad, images), False)
    for ad in movable:
        image, ready = assignments[(ad['ClusterId'], ad['ProcId'])]
        if image == worker_image(ad, images) and ad.get('EpicprodImageReady', True) == ready:
            continue
        job = str(ad['ClusterId']) + '.' + str(ad['ProcId'])
        edits.append({'job': job, 'image': image, 'ready': ready,
                      'applied': edit_image(ad, image, ready, base) if args.apply else False})
    now = datetime.utcnow()
    state = {'schema_version': 1, 'mode': 'active' if args.apply else 'shadow',
             'queue': policy['queue'], 'images': images,
             'observed_at': now.strftime('%Y-%m-%dT%H:%M:%SZ'),
             'valid_until': (now + timedelta(seconds=policy['state_valid_seconds'])).strftime('%Y-%m-%dT%H:%M:%SZ'),
             'demand': demand, 'running': dict(running),
             'waiting': len(waiting), 'waiting_targets': desired,
             'protected_waiting': dict(protected), 'edits': edits,
             'demand_fetch_ms': fetched_ms, 'demand_query_ms': demand_record.get('query_ms')}
    state['resource_limits'] = limits
    if args.apply:
        path = Path(args.state)
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(state, indent=2) + '\n')
        os.chmod(str(temporary), 0o644)
        os.replace(str(temporary), str(path))
    print(json.dumps(state), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--policy', required=True)
    parser.add_argument('--state', required=True)
    parser.add_argument('--env', required=True)
    parser.add_argument('--apply', action='store_true')
    run(parser.parse_args())
