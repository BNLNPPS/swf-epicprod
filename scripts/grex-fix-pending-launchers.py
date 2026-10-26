#!/usr/bin/env python3
"""Atomically repair owned, still-pending CE launcher copies without requeueing."""
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
rows = json.loads(Path(sys.argv[1]).read_text())
replacement = Path(sys.argv[2]).read_bytes()
if b'export _CONDOR_SCRATCH_DIR=$TOP' not in replacement or b'EPICPROD_CE_SANDBOX' not in replacement:
    raise RuntimeError('Replacement does not carry the approved worker relocation')
apply = '--apply' in sys.argv[3:]
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
    if hashlib.sha256(original).hexdigest() != OLD:
        continue
    try:
        body = subprocess.check_output(['scontrol', 'write', 'batch_script', jid, '-'], universal_newlines=True, timeout=10)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        continue
    if 'epicprod-multipilot.sh' not in body:
        print(json.dumps({'slurm': jid, 'skipped': 'Batch script does not reference launcher'}), flush=True)
        continue
    record = dict(slurm=jid, path=str(path), before=OLD, after=hashlib.sha256(replacement).hexdigest(),
                  slurm_before=fields, apply=apply)
    (root / (jid + '.before.sh')).write_bytes(original)
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
        temp = path.with_name('.epicprod-launcher-' + uuid.uuid4().hex)
        temp.write_bytes(replacement); temp.chmod(stat.S_IMODE(path.stat().st_mode))
        os.replace(str(temp), str(path))
        record['slurm_after'] = subprocess.check_output(['scontrol', 'show', 'job', jid, '-o'], universal_newlines=True, timeout=10)
        (root / (jid + '.json')).write_text(json.dumps(record))
    changed += 1
    print(json.dumps(record), flush=True)
print(json.dumps({'summary': True, 'launchers': changed, 'apply': apply, 'journal': str(root)}), flush=True)
