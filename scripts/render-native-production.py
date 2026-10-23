#!/usr/bin/env python3
"""Render the OSG Harvester direct-pilot template without installing it.

Derive exclusions, resource requests, Harvester identity and logging from the
live ALRB template. The operator verifies queue image compatibility before
installing the result. Assets must be staged in an immutable directory.
"""
import argparse
from pathlib import Path
import re


def replace_line(text, key, value):
    pattern = r'(?m)^' + re.escape(key) + r'\s*=.*$'
    result, count = re.subn(pattern, lambda _: key + ' = ' + value, text)
    if count != 1:
        raise ValueError('expected exactly one ' + key)
    return result


def render(text, assets, image):
    if not assets.startswith('/') or not image.startswith('/cvmfs/'):
        raise ValueError('absolute asset directory and CVMFS image required')
    if any(c in assets + image for c in '\n\r\"{} '):
        raise ValueError('unsupported path character')
    if 'EpicprodNative' in text:
        raise ValueError('input is already native; review instead of reapplying')
    text = '\n'.join(x for x in text.splitlines() if not x.startswith('#arguments =')) + '\n'
    text = replace_line(text, 'executable', assets + '/epic-native-pilot.sh')
    # These are pilot.py arguments, not the old wrapper's bootstrap arguments.
    args = ('-q {pandaQueueName} -i {pilotType} -j {prodSourceLabel} -t -w generic '
            '--pilot-user epic --url https://pandaserver01.sdcc.bnl.gov -p 25443 -d '
            '--harvester-submit-mode PULL --allow-same-user=False --job-type={jobType} '
            '{pilotResourceTypeOption} --use-rucio-traces False '
            '--rucio-host https://nprucio01.sdcc.bnl.gov:443 '
            '--queuedata-url http://pandaserver01.sdcc.bnl.gov:25080/cache/schedconfig/{pandaQueueName}.all.json '
            '--storagedata-url https://datalake-cric.cern.ch/cache/ddmendpoints.json '
            '--getjobrequests 30 --getjobfailures 30')
    text = replace_line(text, 'arguments', args)
    match = re.search(r'(?m)^environment\s*=\s*"([^"\n]*)"$', text)
    if not match:
        raise ValueError('expected quoted production environment')
    env = match.group(1)
    for key in ('PANDA_JSID=harvester-', 'HARVESTER_ID=', 'HARVESTER_WORKER_ID=', 'GTAG='):
        if key not in env:
            raise ValueError('missing production identity: ' + key)
    env += (' EPICPROD_NATIVE_IMAGE=' + image + ' EPICPROD_NATIVE_ARCHIVE=pilot3-native.tar.gz'
            ' EPICPROD_NATIVE_NESTING=off')
    text = replace_line(text, 'environment', '"' + env + '"')
    requirements = re.search(r'(?m)^Requirements\s*=\s*(.*)$', text).group(1)
    for clause in ('(HAS_CVMFS_atlas_cern_ch == True) && ',
                   '(OSGVO_OS_STRING == "RHEL 9") && ',
                   '(HAS_UNPRIVILEGED_USER_NAMESPACES =?= "enabled") && '):
        if requirements.count(clause) != 1:
            raise ValueError('unexpected live requirements: ' + clause)
        requirements = requirements.replace(clause, '')
    if 'MY.EpicprodNodeGuard' not in requirements:
        raise ValueError('missing node guard')
    requirements = ('(HasSingularity =?= True) && '
                    '(HAS_CVMFS_singularity_opensciencegrid_org == True) && ' + requirements)
    text = replace_line(text, 'Requirements', requirements)
    inputs = ','.join(assets + '/' + x for x in
                      ('pilot3-native.tar.gz', 'certificates.tar.gz', 'rucio.cfg',
                       'glidein-deadline.sh', 'job-user.sh'))
    additions = ('transfer_input_files = ' + inputs + '\ntransfer_output_files = ""\n'
                 '+WantSingularity = True\n+SingularityImage = "' + image + '"\n'
                 '+EpicprodNative = "production-20261023"\n'
                 '+EpicprodNativeMode = "direct"\n+EpicprodNativeImage = "' + image + '"\n')
    if len(re.findall(r'(?m)^queue\s+1\s*$', text)) != 1:
        raise ValueError('expected one worker per Harvester submission')
    return re.sub(r'(?m)^queue\s+1\s*$', lambda _: additions + '\nqueue 1\n', text)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--template', required=True)
    parser.add_argument('--assets', required=True)
    parser.add_argument('--image', required=True)
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    Path(args.out).write_text(render(Path(args.template).read_text(), args.assets, args.image))
