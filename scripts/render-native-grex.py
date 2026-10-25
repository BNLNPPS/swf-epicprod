#!/usr/bin/env python3
"""Render GREX's Harvester template for the native pilot without installing it
(docs/NATIVE_PILOT_CANARY.md, GREX).

From the live template (submit_pilot3_pull_umgrex.sdf on pandaharvester01):
epicprod-multipilot.sh stays the executable, from the frozen asset directory;
its eight wrappers become the native launcher (EPICPROD_STANDARD_WRAPPER), which
starts the campaign image itself on the batch node (EPICPROD_NATIVE_BATCH=1);
the pilot archive, its helpers and the robot proxy travel as inputs; the
arguments become pilot.py's. Harvester identity, the CE, the allocation policy,
xcount, maxMemory, maxWallTime and the periodic expressions are kept. With
--canary the Harvester placeholders are filled for one hand submission, which
reports to PanDA without a Harvester worker.
"""
import argparse
import importlib.util
import json
from pathlib import Path
import re

_spec = importlib.util.spec_from_file_location(
    'render_native_production', Path(__file__).with_name('render-native-production.py'))
_prod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_prod)
replace_line = _prod.replace_line

ASSETS = ('epicprod-multipilot.sh', 'epic-native-pilot.sh', 'pilot3-native.tar.gz', 'certificates.tar.gz',
          'rucio.cfg', 'glidein-deadline.sh', 'job-user.sh', 'runtime-policy.py', 'queue-runtime-policy.json')
PROXY = '/data/atlpan/proxy/x509up_u1000'
PILOT_ARGS = ('-q {pandaQueueName} -i {pilotType} -j {prodSourceLabel} -t -w generic '
              '--pilot-user epic --url https://pandaserver01.sdcc.bnl.gov -p 25443 -d '
              '--harvester-submit-mode PULL --allow-same-user=False --job-type={jobType} '
              '{pilotResourceTypeOption} --use-rucio-traces False '
              '--rucio-host https://nprucio01.sdcc.bnl.gov:443 '
              '--storagedata-url https://datalake-cric.cern.ch/cache/ddmendpoints.json')


def render(text, assets, image, tag):
    if not assets.startswith('/') or not image.startswith('/cvmfs/'):
        raise ValueError('absolute asset directory and CVMFS image required')
    if any(c in assets + image + tag for c in '\n\r"{} '):
        raise ValueError('unsupported path character')
    if 'EpicprodNative' in text:
        raise ValueError('input is already native; review instead of reapplying')
    executable = re.search(r'(?m)^executable\s*=\s*(.*)$', text).group(1).strip()
    if Path(executable).name != 'epicprod-multipilot.sh':
        raise ValueError('expected the eight-pilot launcher as the executable, found ' + executable)
    text = replace_line(text, 'executable', assets + '/epicprod-multipilot.sh')
    inputs = re.search(r'(?m)^transfer_input_files\s*=\s*(.*)$', text).group(1).split(',')
    queuedata = [x for x in inputs if x.endswith('/queuedata.json')]
    if len(queuedata) != 1:
        raise ValueError('expected one shipped queuedata.json')
    text = replace_line(text, 'transfer_input_files',
                        ','.join([assets + '/' + x for x in ASSETS[1:]] + queuedata + [PROXY]))
    text = replace_line(text, 'arguments', PILOT_ARGS)
    match = re.search(r'(?m)^environment\s*=\s*"([^"\n]*)"$', text)
    if not match:
        raise ValueError('expected quoted environment')
    env = match.group(1)
    for key in ('PANDA_JSID=harvester-', 'HARVESTER_ID=', 'HARVESTER_WORKER_ID=', 'GTAG=',
                'EPICPROD_ALLOCATION_SECONDS=', 'EPICPROD_DRAIN_RESERVE_SECONDS='):
        if key not in env:
            raise ValueError('missing from the environment: ' + key)
    env += (' EPICPROD_STANDARD_WRAPPER=epic-native-pilot.sh EPICPROD_NATIVE_BATCH=1'
            ' EPICPROD_NATIVE_IMAGE=' + image + ' EPICPROD_NATIVE_ARCHIVE=pilot3-native.tar.gz'
            ' EPICPROD_NATIVE_DISPATCH=per-job EPICPROD_NATIVE_NESTING=on'
            ' EPICPROD_NATIVE_PROXY=' + Path(PROXY).name)
    text = replace_line(text, 'environment', '"' + env + '"')
    if len(re.findall(r'(?m)^queue\s+1\s*$', text)) != 1:
        raise ValueError('expected one worker per Harvester submission')
    additions = ('+EpicprodNative = "' + tag + '"\n+EpicprodNativeMode = "per-job"\n'
                 '+EpicprodNativeImage = "' + image + '"\n')
    return re.sub(r'(?m)^queue\s+1\s*$', lambda _: additions + '\nqueue 1\n', text)


def canary(text, directory, wall_minutes, token):
    """The rendered template filled for one hand submission from ``directory``."""
    values = {
        '{computingSite}': 'UM_GREX_PanDA_1', '{pandaQueueName}': 'UM_GREX_PanDA_1',
        '{prodSourceLabel}': 'managed', '{pilotType}': 'PR', '{jobType}': 'managed',
        '{pilotResourceTypeOption}': '--resource-type SCORE --noworkerpilotstatusupdate',
        '{accessPoint}': directory, '{logDir}': directory, '{logSubdir}': 'logs',
        '{harvesterID}': 'native-grex-canary', '{workerID}': '0',
        '{gtag}': 'native-grex-canary', '{ioIntensity}': '0', '{sdfPath}': directory + '/pilot.sdf',
        '{ceHostname}': 'grex-osg-ce-01.hpc.umanitoba.ca',
        '{ceEndpoint}': 'grex-osg-ce-01.hpc.umanitoba.ca:9619',
        '{x509UserProxy}': PROXY, '{tokenPath}': token,
        '$ENV(HARVESTER_CRIC_URL_DDMENDPOINTS)': 'https://datalake-cric.cern.ch/cache/ddmendpoints.json',
    }
    for key, value in values.items():
        text = text.replace(key, value)
    text = re.sub(r'(?m)^\+maxWallTime\s*=.*$', '+maxWallTime = ' + str(int(wall_minutes)), text)
    text = re.sub(r'EPICPROD_ALLOCATION_SECONDS=\d+', 'EPICPROD_ALLOCATION_SECONDS=' + str(int(wall_minutes) * 60), text)
    live = '\n'.join(x for x in text.splitlines() if not x.lstrip().startswith('#'))
    left = sorted(set(re.findall(r'\{[A-Za-z]+\}', live)))
    if left:
        raise ValueError('unfilled placeholders: ' + ', '.join(left))
    return text


def es_canary(text, directory, taskid, channel, cores=2):
    """One task-bound test pilot with cores reserved for its ES slots.

    Task binding restricts this pilot's requests; other pilots can still take
    the task. GREX generates SCORE jobs even when the task requests more cores.
    """
    if int(taskid) < 1 or int(cores) < 2:
        raise ValueError('an ES canary needs a task id and at least two cores')
    if not channel.startswith('/cvmfs/') or any(c in channel for c in '\n\r" {}'):
        raise ValueError('absolute CVMFS channel path required')
    allocation = int(re.search(r'(?m)^\+xcount\s*=\s*(\d+)\s*$', text).group(1))
    memory = int(re.search(r'(?m)^\+maxMemory\s*=\s*(\d+)\s*$', text).group(1))
    if allocation < 1 or memory % allocation:
        raise ValueError('expected an integer memory allowance per core')
    text = replace_line(text, '+xcount', str(cores))
    text = replace_line(text, '+maxMemory', str(memory // allocation * cores))
    executable = re.search(r'(?m)^executable\s*=\s*(.*)$', text).group(1).strip()
    text = replace_line(text, 'executable', str(Path(executable).with_name('epic-native-pilot.sh')))
    text = text.replace('--allow-same-user=False', '--allow-same-user=True')
    text = text.replace('-j managed ', '-j test ').replace('--job-type=managed', '--job-type=test')
    env = re.search(r'(?m)^environment\s*=\s*"([^"\n]*)"$', text).group(1)
    env += (f' PanDA_TaskID={int(taskid)} EPICPROD_MIN_TIME_FOR_NEW_JOB=1200'
            f' EPICPROD_ES_CHANNEL={channel}'
            f' PILOT_ES_EXECUTOR_TYPE=generic'
            f' ATHENA_PROC_NUMBER={int(cores)}')
    text = replace_line(text, 'environment', '"' + env + '"')
    inputs = re.search(r'(?m)^transfer_input_files\s*=\s*(.*)$', text).group(1).split(',')
    inputs = [p for p in inputs if p != str(Path(executable).with_name('epic-native-pilot.sh'))]
    queuefiles = [p for p in inputs if p.endswith('/queuedata.json')]
    if len(queuefiles) != 1:
        raise ValueError('expected one source queue-data file')
    queue = json.loads(Path(queuefiles[0]).read_text())
    queue.update(timefloor=0, maxtime=int(re.search(r'(?m)^\+maxWallTime\s*=\s*(\d+)', text).group(1)) * 60)
    for key in ('acopytools', 'astorages'):
        activities = queue[key]
        for activity in ('es_events', 'es_failover'):
            activities[activity] = list(activities['pw'])
    dest = Path(directory) / 'queuedata.json'
    content = json.dumps(queue, indent=2) + '\n'
    if dest.exists():
        if dest.read_text() != content:
            raise ValueError('refusing to replace different canary queue data')
    else:
        with dest.open('x') as f:
            f.write(content)
    inputs[inputs.index(queuefiles[0])] = str(dest)
    return replace_line(text, 'transfer_input_files', ','.join(inputs))


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('template')
    ap.add_argument('--assets', required=True)
    ap.add_argument('--image', default='/cvmfs/singularity.opensciencegrid.org/eicweb/eic_xl:26.07.3-stable')
    ap.add_argument('--tag', required=True)
    ap.add_argument('--canary-dir', help='fill the placeholders for one hand submission from this directory')
    ap.add_argument('--wall-minutes', type=int, default=240)
    ap.add_argument('--token', default='/data/atlpan/tokens/ce/prod/112106b0e5d956dff67942864654eb41')
    ap.add_argument('--canary-es-task', type=int, help='bind one Event Service pilot to this test task')
    ap.add_argument('--canary-es-channel', help='published Python-matched yampl channel directory on CVMFS')
    ap.add_argument('--canary-es-cores', type=int, default=2)
    a = ap.parse_args()
    out = render(Path(a.template).read_text(), a.assets, a.image, a.tag)
    if a.canary_dir:
        out = canary(out, a.canary_dir, a.wall_minutes, a.token)
    if a.canary_es_task or a.canary_es_channel:
        if not a.canary_dir or not a.canary_es_task or not a.canary_es_channel:
            ap.error('an ES canary needs --canary-dir, --canary-es-task and --canary-es-channel')
        out = es_canary(out, a.canary_dir, a.canary_es_task, a.canary_es_channel, a.canary_es_cores)
    print(out, end='')
