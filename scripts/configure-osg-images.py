#!/usr/bin/env python3
"""Install the approved image allocator after its real-worker canary passes.

Run as root on osgsub01. --apply retains backups and installs the two-minute
cron; otherwise no file or worker changes. Harvester configuration is unchanged.
"""
import argparse
import datetime
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess


def write(path, content, uid, gid, mode):
    temporary = Path(str(path) + '.next')
    temporary.write_text(content)
    os.chown(str(temporary), uid, gid)
    os.chmod(str(temporary), mode)
    os.replace(str(temporary), str(path))


def main(args):
    source = Path(args.source)
    assets = Path(args.assets)
    policy = json.loads((source / 'image-policy.json').read_text())
    template = Path(policy['template_file'])
    text = template.read_text()
    if ('EpicprodNativeMode = "direct"' not in text
            or 'TARGET.GLIDEIN_ToDie' not in text or 'MY.EpicprodNodeGuard' not in text):
        raise ValueError('expected current native template with lifetime and node guards')
    policy_file = str(assets / 'image-policy.json')
    gate = '(MY.EpicprodImageReady =!= False)'
    requirements = re.search(r'(?m)^Requirements\s*=\s*(.*)$', text)
    if gate not in requirements.group(1):
        text = text[:requirements.start(1)] + '(' + requirements.group(1) + ') && ' + gate + text[requirements.end(1):]
    additions = ('+EpicprodImagePolicyFile = "' + policy_file + '"\n'
                 '+EpicprodImageReady = False\n')
    if '+EpicprodImagePolicyFile' in text:
        if '+EpicprodImagePolicyFile = "' + policy_file + '"' not in text:
            raise ValueError('review existing image policy path')
    else:
        if len(re.findall(r'(?m)^queue\s+1\s*$', text)) != 1:
            raise ValueError('expected one worker per Harvester submission')
        text = re.sub(r'(?m)^queue\s+1\s*$', additions + '\nqueue 1', text)
    print('template', str(template), 'allocator', str(assets), 'shared waiting budget', policy['waiting_limit'])
    if not args.apply:
        return
    account = pwd.getpwnam('wenauseic')
    assets.mkdir(parents=True, exist_ok=True)
    os.chown(str(assets), account.pw_uid, account.pw_gid)
    os.chmod(str(assets), 0o755)
    stamp = datetime.datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')
    for name in ('image-policy.json', 'image-allocation.py'):
        write(assets / name, (source / name).read_text(), 0, 0, 0o644 if name.endswith('.json') else 0o755)
    shutil.copy2(str(template), str(template) + '.before-images-' + stamp)
    st = template.stat()
    write(template, text, st.st_uid, st.st_gid, st.st_mode & 0o777)
    base = ('Owner == "atlpan" && harvesterID == "BNL_osg_harvester_1" && '
            'EpicprodNativeMode == "direct" && JobStatus == 1')
    raw = subprocess.check_output(['condor_q', '-allusers', '-constraint', base,
                                   '-long', '-attributes', 'ClusterId,ProcId,Requirements'], universal_newlines=True)
    before = assets / ('idle-before-images-' + stamp + '.ads')
    write(before, raw, 0, 0, 0o600)
    groups = {}
    for block in re.split(r'\n\s*\n', raw.strip()):
        values = dict(re.findall(r'(?m)^(\w+) = (.*)$', block))
        if values and 'EpicprodImageReady' not in values['Requirements']:
            groups.setdefault(values['Requirements'], []).append(values['ClusterId'] + '.' + values['ProcId'])
    for req, ids in groups.items():
        print(subprocess.check_output(['condor_qedit', '-jobids', ','.join(ids), '-constraint', base,
                                       'Requirements', '(' + req + ') && ' + gate], universal_newlines=True).strip())
    cron = ('# Native OSG image allocation; Harvester owns the shared waiting cap.\n'
            '*/2 * * * * wenauseic /usr/bin/flock -n ' + str(assets / 'allocation.lock')
            + ' /usr/bin/python3 ' + str(assets / 'image-allocation.py')
            + ' --policy ' + policy_file + ' --state ' + str(assets / 'allocation-state.json')
            + ' --env /eic/u/wenauseic/.swf-harvester-reporter.env --apply'
            + ' >>' + str(assets / 'allocation.log') + ' 2>&1\n')
    cron_path = Path('/etc/cron.d/osg-image-allocation')
    if cron_path.exists():
        shutil.copy2(str(cron_path), str(cron_path) + '.before-' + stamp)
    write(cron_path, cron, 0, 0, 0o644)
    print('installed', stamp, 'running workers unchanged')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--assets', default='/var/data/atlpan/harvester_common/image-allocation-20261024')
    parser.add_argument('--apply', action='store_true')
    main(parser.parse_args())
