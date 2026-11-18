#!/usr/bin/env python3
"""Render JLAB_PanDA_1's Harvester template on the direct (native) pilot.

JLab's earlier launcher (pilot/jlab-native-pilot.sh) ran the standard BNL
wrapper, which downloads the stock pilot; its container setup requires
/cvmfs/atlas.cern.ch, absent on the JLab farm (canary job 6608533,
2026-11-17: pilot error 1186). This takes GREX's live native template
(docs/NATIVE_PILOT_CANARY.md) and keeps from it the native runtime: the
frozen asset directory, the launcher, the pilot archive and helpers, the
native environment and the pilot arguments. From JLab's live template it
keeps what is JLab's: the CE and its SciTokens authentication with the
pilot proxy as an ordinary input, one core, memory, the Slurm walltime and
the queue data. The multipilot launcher runs one pilot per batch job.

    render-native-jlab.py GREX.sdf JLAB.sdf [--max-memory MB] > NEW.sdf
"""
import argparse
import re
import sys

QUEUE_DATA = '/data/atlpan/harvester_common/queuedata/JLAB_PanDA_1/queuedata.json'
PROXY = '/data/atlpan/proxy/x509up_u1000'
POLICY = 'jlab-24h-20261117'


def value(text, key):
    match = re.search(rf'^{re.escape(key)}\s*=\s*(.*)$', text, re.M)
    if not match:
        raise ValueError(f'no {key} line')
    return match.group(1).strip()


def render(grex, jlab, max_memory=None):
    assets = value(grex, 'executable').rsplit('/', 1)[0]
    inputs = [f for f in value(grex, 'transfer_input_files').split(',')
              if 'queuedata' not in f and f != PROXY]
    if not all(f.startswith(assets + '/') for f in inputs):
        raise ValueError('GREX inputs outside its asset directory')
    inputs += [QUEUE_DATA, PROXY]
    walltime_minutes = int(value(jlab, '+maxWallTime'))
    env = value(grex, 'environment').strip('"')
    fields = dict(item.split('=', 1) for item in env.split())
    fields.update({
        'EPICPROD_PILOTS_PER_SLOT': '1',
        'EPICPROD_ALLOCATION_SECONDS': str(walltime_minutes * 60),
        'EPICPROD_LIFETIME_POLICY': POLICY,
    })
    environment = ' '.join(f'{k}={v}' for k, v in fields.items())
    tag = value(grex, '+EpicprodNative').strip('"')
    lines = [
        f'executable = {assets}/epicprod-multipilot.sh',
        f'transfer_input_files = {",".join(inputs)}',
        '',
        f'arguments = {value(grex, "arguments")}',
        '',
        'initialdir = {accessPoint}',
        '',
        'log = {logDir}/{logSubdir}/grid.$(Cluster).$(Process).log',
        'output = {logDir}/{logSubdir}/grid.$(Cluster).$(Process).out',
        'error = {logDir}/{logSubdir}/grid.$(Cluster).$(Process).err',
        'transfer_executable = True',
        '',
        f'environment = "{environment}"',
        '+harvesterID = "{harvesterID}"',
        '+harvesterWorkerID = "{workerID}"',
        '',
        'universe = grid',
        '#grid_resource = condor osg-ce-2.jlab.org osg-ce-2.jlab.org:9619',
        'grid_resource = condor {ceHostname} {ceEndpoint}',
        '',
        '# Pilot proxy travels as a regular protected input; CE authentication is SciTokens.',
        'ShouldTransferFiles = YES',
        'WhenToTransferOutput = ON_EXIT',
        'use_x509userproxy = false',
        '',
        '+remote_jobuniverse = 5',
        '+remote_ShouldTransferFiles = "YES"',
        '+remote_WhenToTransferOutput = "ON_EXIT_OR_EVICT"',
        '+remote_TransferOutput = ""',
        '+ioIntensity = {ioIntensity}',
        f'+xcount = {value(jlab, "+xcount")}',
        f'+maxMemory = {max_memory or value(jlab, "+maxMemory")}',
        f'+maxWallTime = {walltime_minutes}',
        '# minutes; JLab Slurm rejects a pilot without one',
        '',
        'delegate_job_GSI_credentials_lifetime = 0',
        '',
        f'periodic_remove = {value(jlab, "periodic_remove")}',
        f'+remote_PeriodicRemove = {value(jlab, "+remote_PeriodicRemove")}',
        '',
        '+sdfPath = "{sdfPath}"',
        '+ScitokensFile = "{tokenPath}"',
        '',
        '+PandaJob = true',
        '',
        f'+EpicprodNative = "jlab-{tag.split("-", 1)[1] if tag.startswith("grex-") else tag}"',
        f'+EpicprodNativeMode = {value(grex, "+EpicprodNativeMode")}',
        f'+EpicprodNativeImage = {value(grex, "+EpicprodNativeImage")}',
        '',
        'queue 1',
    ]
    return '\n'.join(lines) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('grex', help="GREX's live template")
    parser.add_argument('jlab', help="JLab's live template")
    parser.add_argument('--max-memory', type=int,
                        help="MB per pilot; default JLab's current value. The queue "
                             "record's maxrss (docs/SCHEDCONFIG_OVERRIDES.md) is what "
                             "PanDA dispatches against")
    args = parser.parse_args()
    with open(args.grex) as g, open(args.jlab) as j:
        sys.stdout.write(render(g.read(), j.read(), args.max_memory))


if __name__ == '__main__':
    main()
