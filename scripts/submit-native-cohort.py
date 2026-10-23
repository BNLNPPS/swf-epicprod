#!/usr/bin/env python3
"""Submit a cohort of native ePIC pilots to the OSG pool, outside Harvester.

The cohort's submit description is derived from the live production pull
template so that it carries the same exclusions, undesired sites, node-guard
clause, resource requests and priority class; it differs in the executable
(pilot/native/epic-native-pilot.sh), the transferred archive, the requested
image (+SingularityImage) and the requirements: HasSingularity is required,
the ATLAS CVMFS and host RHEL 9 requirements are dropped, unprivileged user
namespaces are kept. Pilots carry the tag in PANDA_JSID, GTAG (their stdout
URL) and the ad attribute EpicprodNative; with --task they acquire only that
task's jobs (the baseline pilot's PanDA_TaskID with --allow-same-user=True).
+MaxRuntime = 360 and the running bound of periodic_remove are the production
template's own values (requestWalltimeMinute resolves to 360 there); an idle
trial pilot is removed after twelve hours. Without --submit the description is
written and printed, nothing is submitted.
docs/NATIVE_PILOT_CANARY.md § Staged trial, 23 October.
"""
import argparse
import datetime
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

PILOT_ARGS = ('-q {queue} -i PR -j unified -t -w generic --pilot-user epic '
              '--url https://pandaserver01.sdcc.bnl.gov -p 25443 -d '
              '--harvester-submit-mode PULL --allow-same-user={same_user} '
              '--job-type=managed --resource-type SCORE --use-rucio-traces False '
              '--rucio-host https://nprucio01.sdcc.bnl.gov:443 '
              '--queuedata-url http://pandaserver01.sdcc.bnl.gov:25080/cache/schedconfig/{queue}.all.json '
              '--storagedata-url https://datalake-cric.cern.ch/cache/ddmendpoints.json '
              '--getjobrequests 30 --getjobfailures 30 --noworkerpilotstatusupdate')
DROPPED = ('(HAS_CVMFS_atlas_cern_ch == True) && ', '(OSGVO_OS_STRING == "RHEL 9") && ')
LOG_URL = 'https://osgsub01.sdcc.bnl.gov/condor_logs/{logdir}/pilot.$(Cluster).$(Process).out'
# The outer container's /var/lib/apptainer, so the OSG Apptainer can nest another
# image from inside this one (proven on osgsub01, 23 October 2026).
SESSION_BIND = '/cvmfs/oasis.opensciencegrid.org/mis/apptainer/1.4.5/x86_64/var/lib/apptainer:/var/lib/apptainer:ro'
IMAGES = {'26.07.1': '/cvmfs/singularity.opensciencegrid.org/eicweb/eic_xl:26.07.1-stable',
          '26.07.3': '/cvmfs/singularity.opensciencegrid.org/eicweb/eic_xl:26.07.3-stable'}


def line(text, key):
    found = re.findall(r'(?m)^' + re.escape(key) + r'\s*=\s*(.*)$', text)
    if len(found) != 1:
        raise ValueError(f'expected one {key} line in the production template, found {len(found)}')
    return found[0].strip()


def describe(template, args):
    text = Path(template).read_text()
    requirements = line(text, 'Requirements')
    for clause in DROPPED:
        if clause not in requirements:
            raise ValueError(f'production requirements lack {clause!r}; review before deriving')
        requirements = requirements.replace(clause, '')
    # The repositories the pilot uses: the image, the runtime wrapper and
    # certificates, rucio.cfg. Nodes without CVMFS advertise HasSingularity
    # too and fail at image preparation (23 October: Missouri-Hellbender,
    # Duke-NCShare); the ATLAS requirement used to hide this.
    requirements = ('(HasSingularity =?= True) && (HAS_CVMFS_singularity_opensciencegrid_org == True) && '
                    '(HAS_CVMFS_oasis_opensciencegrid_org == True) && (HAS_CVMFS_eic_opensciencegrid_org == True) && '
                    + requirements)
    if 'HAS_UNPRIVILEGED_USER_NAMESPACES' not in requirements or 'EpicprodNodeGuard' not in requirements:
        raise ValueError('derived requirements lost the user-namespace or node-guard clause')
    undesired = line(text, '+UNDESIRED_Sites')
    guard = line(text, '+EpicprodNodeGuard')
    memory = line(text, 'request_memory')
    disk = line(text, 'request_disk')
    category = line(text, '+JobDurationCategory')
    periodic_remove = line(text, 'periodic_remove')  # the production running bound; healthy jobs finish
    pilot_args = PILOT_ARGS.format(queue=args.queue, same_user='True' if args.task else 'False')
    environment = [f'EPICPROD_NATIVE_IMAGE={args.image}', 'EPICPROD_NATIVE_ARCHIVE=' + args.archive,
                   f'EPICPROD_NATIVE_PREFLIGHT_IMAGE={args.preflight_image}',
                   # glideinWMS's singularity_lib.sh takes extra binds from these, not from +SingularityBind
                   'APPTAINER_BINDPATH=' + SESSION_BIND, 'SINGULARITY_BINDPATH=' + SESSION_BIND,
                   f'PANDA_JSID={args.tag}', 'GTAG=' + LOG_URL.format(logdir=args.logdir)]
    if args.task:
        environment.append(f'PanDA_TaskID={args.task}')
    return f'''# Native ePIC pilot cohort {args.tag}, generated {datetime.datetime.now(datetime.timezone.utc):%Y-%m-%dT%H:%M:%SZ}
# by scripts/submit-native-cohort.py from {template}
universe = vanilla
executable = {args.dir}/epic-native-pilot.sh
transfer_executable = True
should_transfer_files = YES
when_to_transfer_output = ON_EXIT
transfer_output_files = ""
transfer_input_files = {args.dir}/{args.archive}
x509userproxy = {args.proxy}
initialdir = {args.dir}
output = /var/data/atlpan/condor_logs/{args.logdir}/pilot.$(Cluster).$(Process).out
error = /var/data/atlpan/condor_logs/{args.logdir}/pilot.$(Cluster).$(Process).err
log = /var/data/atlpan/condor_logs/{args.logdir}/pilot.$(Cluster).$(Process).log
environment = "{' '.join(environment)}"
arguments = {pilot_args}
+JobDurationCategory = {category}
request_cpus = 1
request_memory = {memory}
request_disk = {disk}
priority = {args.priority}
+MaxRuntime = 360
+ProjectName = "EIC"
+WantSingularity = True
+SingularityImage = "{args.image}"
+SingularityBind = "{SESSION_BIND}"
Requirements = {requirements}
+UNDESIRED_Sites = {undesired}
+EpicprodNodeGuard = {guard}
+EpicprodNative = "{args.tag}"
+EpicprodNativeImage = "{args.image}"
periodic_remove = {periodic_remove} || (JobStatus == 1 && (CurrentTime - QDate) > 43200)
queue {args.count}
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--image', required=True, help='the campaign image, an absolute CVMFS path')
    parser.add_argument('--count', type=int, default=1)
    parser.add_argument('--task', type=int, default=0, help='acquire only this task\'s jobs')
    parser.add_argument('--tag', default='epic-native-' + datetime.date.today().strftime('%Y%m%d'))
    parser.add_argument('--queue', default='BNL_OSG_PanDA_1')
    parser.add_argument('--dir', default='/var/data/atlpan/native-20261023')
    parser.add_argument('--logdir', default='native-20261023', help='under /var/data/atlpan/condor_logs')
    parser.add_argument('--archive', default='pilot3-native.tar.gz')
    parser.add_argument('--proxy', default='/var/data/atlpan/proxy/x509up_u1000')
    parser.add_argument('--priority', type=int, default=2, help='above the production pilots\' 1')
    parser.add_argument('--template', default='/var/data/atlpan/harvester_common/submit_pilot2_pull_bnl_osg.sdf')
    parser.add_argument('--submit', action='store_true', help='condor_submit the description as atlpan')
    parser.add_argument('--preflight-image', help='another campaign image the pilot must nest '
                        'successfully before taking a job; default: the other of the two known images')
    parser.add_argument('--out', help='write the description here instead of under --dir (review off the host)')
    args = parser.parse_args()
    if not args.preflight_image:
        others = [path for path in IMAGES.values() if path != args.image]
        if len(others) != 1:
            sys.exit('give --preflight-image: the image is not one of the two known ones')
        args.preflight_image = others[0]
    if not os.path.isabs(args.image):
        sys.exit(f'image must be an absolute CVMFS path: {args.image}')
    text = describe(args.template, args)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    path = Path(args.out) if args.out else Path(args.dir) / f'{args.tag}-{stamp}.sdf'
    if args.submit:
        if not os.path.isdir(args.image):
            sys.exit(f'image is not a directory on this host: {args.image}')
        for required in (Path(args.dir) / 'epic-native-pilot.sh', Path(args.dir) / args.archive):
            if not required.is_file():
                sys.exit(f'missing {required}')
        Path('/var/data/atlpan/condor_logs', args.logdir).mkdir(exist_ok=True)
    path.write_text(text)
    print(text)
    print(f'# written {path}')
    if args.submit:
        command = ['condor_submit', str(path)]
        if os.geteuid() == 0:  # sudo -u atlpan does not work on osgsub01; su does
            command = ['sudo', 'su', '-s', '/bin/bash', 'atlpan', '-c', 'condor_submit ' + shlex.quote(str(path))]
        print('#', ' '.join(command))
        sys.exit(subprocess.call(command))


if __name__ == '__main__':
    main()
