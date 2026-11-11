#!/usr/bin/env python3
"""Point GREX's live native template at another staged runtime
(docs/NATIVE_PILOT_CANARY.md, GREX).

Runs on pandaharvester01 as root. Every asset path of the template names one
runtime directory under harvester_common; this replaces that directory and
the ``+EpicprodNative`` tag and changes nothing else (resources, lifetime,
CE, Harvester identities stay). The template is read only when its SHA-256
is the one given; every file the new template names must exist. Without
--apply it prints the change. With --apply the old template is kept beside
it as ``<template>.before-<label>-<UTC stamp>`` and the new one replaces it
atomically. Harvester reads the template at each submission; workers already
submitted keep their transferred runtime.

    retarget-native-grex.py --runtime /data/atlpan/harvester_common/native-grex-X \\
        --tag grex-X --label X --expected-sha256 <current> [--apply]
"""
import argparse
import difflib
import hashlib
import os
from pathlib import Path
import re
import shutil
import time

TEMPLATE = '/data/atlpan/harvester_common/submit_pilot3_pull_umgrex.sdf'
ROOT = '/data/atlpan/harvester_common/'
RUNTIME = re.compile(re.escape(ROOT) + r'(native-grex-[A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)')
TAG = re.compile(r'(?m)^\+EpicprodNative\s*=\s*"[^"\n]*"\s*$')


def retarget(text, runtime, tag):
    names = {m.group(1) for m in RUNTIME.finditer(text)}
    if len(names) != 1:
        raise ValueError(f'expected one runtime directory in the template, found {sorted(names)}')
    new_name = Path(runtime).name
    if str(Path(runtime).parent) + '/' != ROOT or not new_name.startswith('native-grex-'):
        raise ValueError('the runtime must be a native-grex-* directory under ' + ROOT)
    if len(TAG.findall(text)) != 1 or not re.fullmatch(r'[A-Za-z0-9._-]+', tag):
        raise ValueError('expected one +EpicprodNative line and a plain tag')
    out = text.replace(ROOT + names.pop() + '/', ROOT + new_name + '/')
    out = TAG.sub(f'+EpicprodNative = "{tag}"', out)
    missing = [m.group(0) for m in RUNTIME.finditer(out) if not os.path.isfile(m.group(0))]
    if missing:
        raise ValueError('files the new template names are missing: ' + ', '.join(missing))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--template', default=TEMPLATE)
    ap.add_argument('--runtime', required=True)
    ap.add_argument('--tag', required=True)
    ap.add_argument('--label', required=True, help='names the backup')
    ap.add_argument('--expected-sha256', required=True)
    ap.add_argument('--apply', action='store_true')
    a = ap.parse_args()
    path = Path(a.template)
    before = path.read_bytes()
    digest = hashlib.sha256(before).hexdigest()
    if digest != a.expected_sha256:
        raise SystemExit(f'template is {digest}, not the expected {a.expected_sha256}; nothing changed')
    after = retarget(before.decode(), a.runtime, a.tag)
    print(''.join(difflib.unified_diff(before.decode().splitlines(True), after.splitlines(True),
                                       str(path), str(path) + ' (new)')), end='')
    print('before', digest)
    print('after ', hashlib.sha256(after.encode()).hexdigest())
    if not a.apply:
        print('plan only; --apply installs it')
        return
    backup = path.with_name(path.name + f'.before-{a.label}-{time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())}')
    shutil.copy2(path, backup)
    tmp = path.with_name(path.name + '.new')
    tmp.write_text(after)
    shutil.copystat(path, tmp)
    st = path.stat()
    os.chown(tmp, st.st_uid, st.st_gid)
    os.replace(tmp, path)
    print('installed; backup', backup)


if __name__ == '__main__':
    main()
