#!/usr/bin/env python3
"""Atomically repair owned, still-pending CE launcher copies without requeueing."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import uuid

OLD = 'e72517516212a5c4bd3b6c621cbc9b39355d2fe567a9393855fceb7c4034b10f'
PILOT_ARCHIVES = {
    '9532f30b41a8b1c8cfcb730eb325933c71c486454ed4365cd3e2d8b625a27ae2',
    '250054c846255a7d9aa059a4e52d0de03eb07e6bc35e563f06e28697bfc0cc41',
}
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('manifest', type=Path)
parser.add_argument('launcher', type=Path)
parser.add_argument('--pilot-archive', type=Path)
parser.add_argument('--archive-sha256')
parser.add_argument('--native-wrapper', type=Path)
parser.add_argument('--apply', action='store_true')
args = parser.parse_args()
rows = json.loads(args.manifest.read_text())
replacement = args.launcher.read_bytes()
base_hash = hashlib.sha256(replacement).hexdigest()
helper = args.native_wrapper.read_bytes() if args.native_wrapper else None
if args.pilot_archive:
    if not helper or hashlib.sha256(helper).hexdigest() != '804b1c64381d47ada0637892843b536d96784df7091cd0283a74e66322498d00':
        raise RuntimeError('Use the validated CE-bind native wrapper with headroom telemetry')
    if base_hash != 'f6119577c52009c8cb279e55662ebb7ccf8db6744b9ce96921692f730abeddcc':
        raise RuntimeError('Unexpected validated relocation launcher')
    # Stage versioned assets first. One atomic entry-file replacement then
    # selects the complete set; a concurrent start cannot mix old/new assets.
    replacement = replacement.replace(b'for name in epic-native-pilot.sh ',
        b'for name in epic-native-pilot-headroom.sh pilot3-native-headroom.tar.gz epic-native-pilot.sh ', 1)
    replacement = replacement.replace(b'#!/bin/bash\n', b'#!/bin/bash\n'
        b'export EPICPROD_STANDARD_WRAPPER=epic-native-pilot-headroom.sh\n'
        b'export EPICPROD_NATIVE_ARCHIVE=pilot3-native-headroom.tar.gz\n', 1)
replacement_hash = hashlib.sha256(replacement).hexdigest()
archive = args.pilot_archive.read_bytes() if args.pilot_archive else None
if archive and (not args.archive_sha256 or hashlib.sha256(archive).hexdigest() != args.archive_sha256):
    raise RuntimeError('Headroom archive hash does not match')
if b'export _CONDOR_SCRATCH_DIR=$TOP' not in replacement or b'EPICPROD_CE_SANDBOX' not in replacement:
    raise RuntimeError('Replacement does not carry the approved worker relocation')
apply = args.apply
uid = os.getuid()
os.environ['PATH'] = '/opt/slurm/bin:' + os.environ.get('PATH', '')
root = Path(os.environ['OSG_WN_TMP']) / ('epicprod-pending-fix-' + uuid.uuid4().hex)
root.mkdir(mode=0o700)
running = set(subprocess.check_output(['squeue', '-h', '-u', str(uid), '-t', 'R', '-o', '%Z'],
                                    universal_newlines=True, timeout=20).splitlines())
pending = set(subprocess.check_output(['squeue', '-h', '-u', str(uid), '-t', 'PD', '-o', '%i'],
                                    universal_newlines=True, timeout=20).splitlines())
seen, changed = set(), 0
for row in rows:
    match = re.search(r' slurm/\d+/(\d+)$', row.get('GridJobId', ''))
    if row.get('Owner') != 'epicprod2' or row.get('Cmd') != 'epicprod-multipilot.sh' or not match:
        continue
    sandbox = Path(row.get('Iwd', ''))
    if not re.fullmatch(r'/var/lib/condor-ce/spool/\d+/\d+/cluster\d+\.proc\d+\.subproc\d+', str(sandbox)):
        continue
    if str(sandbox) in seen or str(sandbox) in running:
        continue
    seen.add(str(sandbox)); jid = match[1]
    if jid not in pending:
        continue
    try:
        raw = subprocess.check_output(['scontrol', 'show', 'job', jid, '-o'], universal_newlines=True, timeout=10)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        continue
    fields = dict(re.findall(r'(?:^|\s)(JobState|UserId|WorkDir|SubmitTime|EligibleTime|Priority)=([^\s]+)', raw))
    owner = re.search(r'\((\d+)\)', fields.get('UserId', ''))
    if fields.get('JobState') != 'PENDING' or not owner or int(owner[1]) != uid or fields.get('WorkDir') != str(sandbox):
        continue
    path = sandbox / 'epicprod-multipilot.sh'
    if path.is_symlink() or not path.is_file() or path.lstat().st_uid != uid or sandbox.lstat().st_uid != uid:
        continue
    original = path.read_bytes()
    original_hash = hashlib.sha256(original).hexdigest()
    if original_hash not in (OLD, base_hash, replacement_hash):
        continue
    archive_path = sandbox / 'pilot3-native.tar.gz'
    original_archive, archive_hash = None, None
    if archive:
        if archive_path.is_symlink() or not archive_path.is_file() or archive_path.stat().st_uid != uid:
            continue
        original_archive = archive_path.read_bytes()
        archive_hash = hashlib.sha256(original_archive).hexdigest()
        if archive_hash not in PILOT_ARCHIVES | {args.archive_sha256}:
            continue
    if original_hash == replacement_hash and (not archive or archive_hash == args.archive_sha256):
        continue
    try:
        body = subprocess.check_output(['scontrol', 'write', 'batch_script', jid, '-'], universal_newlines=True, timeout=10)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        continue
    if 'epicprod-multipilot.sh' not in body:
        print(json.dumps({'slurm': jid, 'skipped': 'Batch script does not reference launcher'}), flush=True)
        continue
    record = dict(slurm=jid, path=str(path), before=original_hash, after=replacement_hash,
                  slurm_before=fields, apply=apply, archive_before=archive_hash,
                  archive_after=args.archive_sha256 if archive else None)
    (root / (jid + '.before.sh')).write_bytes(original)
    if original_archive is not None:
        (root / (jid + '.before.tar.gz')).write_bytes(original_archive)
    (root / (jid + '.json')).write_text(json.dumps(record))
    if apply:
        # Recheck immediately before an atomic single-file replacement. Assets
        # are unchanged; a concurrent start sees one complete approved launcher.
        try:
            current = subprocess.check_output(['scontrol', 'show', 'job', jid, '-o'], universal_newlines=True, timeout=10)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            continue
        if 'JobState=PENDING ' not in current:
            continue
        if archive:
            for name, data in (('pilot3-native-headroom.tar.gz', archive),
                               ('epic-native-pilot-headroom.sh', helper)):
                destination = sandbox / name
                if destination.exists():
                    if destination.is_symlink() or destination.read_bytes() != data:
                        raise RuntimeError('Versioned runtime already exists with different contents')
                else:
                    staged = destination.with_name('.epicprod-asset-' + uuid.uuid4().hex)
                    staged.write_bytes(data); staged.chmod(0o555)
                    os.replace(str(staged), str(destination))
            current = subprocess.check_output(['scontrol', 'show', 'job', jid, '-o'], universal_newlines=True, timeout=10)
            if 'JobState=PENDING ' not in current:
                continue
        temp = path.with_name('.epicprod-launcher-' + uuid.uuid4().hex)
        temp.write_bytes(replacement); temp.chmod(stat.S_IMODE(path.stat().st_mode))
        os.replace(str(temp), str(path))
        record['slurm_after'] = subprocess.check_output(['scontrol', 'show', 'job', jid, '-o'], universal_newlines=True, timeout=10)
        (root / (jid + '.json')).write_text(json.dumps(record))
    changed += 1
    print(json.dumps(record), flush=True)
statv = os.statvfs('/var/lib/condor-ce/spool')
print(json.dumps({'summary': True, 'launchers': changed, 'apply': apply, 'journal': str(root),
                  'ce_free_bytes': statv.f_bavail * statv.f_frsize,
                  'ce_free_inodes': statv.f_favail}), flush=True)
