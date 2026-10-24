#!/usr/bin/env python3
"""Render the OSG Harvester direct-pilot template without installing it.

Derive exclusions, resource requests, Harvester identity and logging from the
live ALRB template. The operator verifies queue image compatibility before
installing the result. Assets must be staged in an immutable directory.
"""
import argparse
import importlib.util
import json
from pathlib import Path
import re


def replace_line(text, key, value):
    pattern = r'(?m)^' + re.escape(key) + r'\s*=.*$'
    result, count = re.subn(pattern, lambda _: key + ' = ' + value, text)
    if count != 1:
        raise ValueError('expected exactly one ' + key)
    return result


def runtime_limits(text):
    native = Path(__file__).resolve().parents[1] / 'pilot/native'
    spec = importlib.util.spec_from_file_location('osg_runtime_policy', native / 'runtime-policy.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    expression = module.condor_maximum(json.loads((native / 'queue-runtime-policy.json').read_text()), 'BNL_OSG_PanDA_1')
    if expression is None:
        return text
    if re.search(r'(?m)^job_machine_attrs\s*=', text):
        raise ValueError('review existing job_machine_attrs before installing site runtime limits')
    text = replace_line(text, '+MaxRuntime', expression + '\njob_machine_attrs = GLIDEIN_Site\njob_machine_attrs_history_length = 1')
    old = re.search(r'(?m)^periodic_remove\s*=\s*(.*)$', text)
    if old is None:
        raise ValueError('missing existing periodic_remove safeguard')
    cap = '(JobStatus == 2 && (CurrentTime - EnteredCurrentStatus) > (60 * MaxRuntime))'
    return replace_line(text, 'periodic_remove', '(' + old.group(1) + ') || ' + cap)


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
    # x86_64-v2 slots (Ivy Bridge and older, no AVX2) run the campaign job
    # about half again slower than Haswell and cross the six-hour wall:
    # 55 of 62 wall failures in the first seven hours of direct-pilot
    # production, 23 October 2026, on Xeon E5-2660 v2 at SU-ITS; 5 percent
    # of the usable cores. The matchmaker leaves them to others.
    # A glidein with under two hours of life left is skipped by the matchmaker
    # instead of being declined by the launcher after landing (23 October
    # 2026: 2,700 declines an hour, mostly Fir, whose glideins live 19,800 s
    # in total). Two hours sits below every site's admission floor, so the
    # launcher's per-site rule stays the exact gate.
    requirements = ('(HasSingularity =?= True) && '
                    '(HAS_CVMFS_singularity_opensciencegrid_org == True) && '
                    '(Microarch =!= "x86_64-v2") && '
                    '(GLIDEIN_ToDie =?= undefined || GLIDEIN_ToDie - time() > 7200) && '
                    + requirements)
    text = replace_line(text, 'Requirements', requirements)
    inputs = ','.join(assets + '/' + x for x in
                      ('pilot3-native.tar.gz', 'certificates.tar.gz', 'rucio.cfg',
                       'glidein-deadline.sh', 'job-user.sh',
                       'runtime-policy.py', 'queue-runtime-policy.json'))
    additions = ('transfer_input_files = ' + inputs + '\ntransfer_output_files = ""\n'
                 '+WantSingularity = True\n+SingularityImage = "' + image + '"\n'
                 '+EpicprodNative = "production-20261023"\n'
                 '+EpicprodNativeMode = "direct"\n+EpicprodNativeImage = "' + image + '"\n')
    if len(re.findall(r'(?m)^queue\s+1\s*$', text)) != 1:
        raise ValueError('expected one worker per Harvester submission')
    return runtime_limits(re.sub(r'(?m)^queue\s+1\s*$', lambda _: additions + '\nqueue 1\n', text))


def retarget(text, assets, tag):
    """Move an existing native template to frozen assets, preserving policy."""
    if (not assets.startswith('/') or any(c in assets for c in '\n\r\"{} ')
            or not re.fullmatch(r'[A-Za-z0-9._-]+', tag)):
        raise ValueError('absolute asset directory and simple deployment tag required')
    executable = re.search(r'(?m)^executable\s*=\s*(.*)$', text)
    if not executable or Path(executable.group(1)).name != 'epic-native-pilot.sh':
        raise ValueError('expected an existing native pilot template')
    old = Path(executable.group(1)).parent
    inputs = re.search(r'(?m)^transfer_input_files\s*=\s*(.*)$', text)
    if not inputs:
        raise ValueError('native template has no input list')
    files = [item.strip() for item in inputs.group(1).split(',')]
    names = ('pilot3-native.tar.gz', 'certificates.tar.gz', 'rucio.cfg',
             'glidein-deadline.sh', 'job-user.sh',
             'runtime-policy.py', 'queue-runtime-policy.json')
    for name in names[:5]:
        if files.count(str(old / name)) != 1:
            raise ValueError('expected exactly one frozen input: ' + name)
    files = [str(Path(assets) / Path(item).name)
             if Path(item).parent == old and Path(item).name in names else item
             for item in files]
    for name in names[5:]:
        if str(Path(assets) / name) not in files:
            files.append(str(Path(assets) / name))
    text = replace_line(text, 'executable', assets + '/epic-native-pilot.sh')
    text = replace_line(text, 'transfer_input_files', ','.join(files))
    return runtime_limits(replace_line(text, '+EpicprodNative', '"' + tag + '"'))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--template', required=True)
    parser.add_argument('--assets', required=True)
    parser.add_argument('--image')
    parser.add_argument('--retarget', action='store_true', help='keep the existing native template policy')
    parser.add_argument('--tag', default='production-runtime-policy')
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    text = Path(args.template).read_text()
    if args.retarget:
        result = retarget(text, args.assets, args.tag)
    else:
        if not args.image:
            parser.error('--image is required unless --retarget is used')
        result = render(text, args.assets, args.image)
    Path(args.out).write_text(result)
