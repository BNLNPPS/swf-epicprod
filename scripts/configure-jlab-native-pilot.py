#!/usr/bin/env python3
"""Fix only JLab's pilot template, with backups and no Harvester restart.

Run as atlpan on pandaharvester01, passing the repository's wrapper file.
CE authentication remains SciTokens. A protected regular file transfer
delivers the proxy to the pilot without the CE's GSI delegation path.
"""
import argparse
import datetime
import os
from pathlib import Path
import shutil
import tempfile


def replace_file(path, data, mode):
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, str(path))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wrapper', type=Path, required=True)
    parser.add_argument('--root', type=Path, default=Path('/data/atlpan/harvester_common'))
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    path = args.root / 'submit_pilot3_pull_jlab.sdf'
    source = path.read_text()
    replacements = [
        ('executable = {executableFile}', 'executable = ' + str(args.root / 'jlab-native-pilot.sh')),
        ('transfer_input_files = ' + str(args.root / 'queuedata/JLAB_PanDA_1/queuedata.json'),
         'transfer_input_files = ' + ','.join(str(p) for p in (
             args.root / 'queuedata/JLAB_PanDA_1/queuedata.json',
             args.root / 'bnlpanda.runpilot2-wrapper.sh', Path('/data/atlpan/proxy/x509up_u1000')))),
        ('X509UserProxy = {x509UserProxy}', '# Pilot proxy travels as a regular protected input; CE authentication is SciTokens.'),
        ('use_x509userproxy = true', 'use_x509userproxy = false'),
    ]
    for old, new in replacements:
        if new in source:
            continue
        if source.count(old) != 1:
            raise ValueError('unsupported JLab template context: ' + old)
        source = source.replace(old, new)
    print(source)
    if args.apply:
        stamp = datetime.datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')
        backup = path.with_name(path.name + '.before-native-' + stamp)
        shutil.copy2(str(path), str(backup))
        replace_file(args.root / 'jlab-native-pilot.sh', args.wrapper.read_bytes(), 0o755)
        replace_file(path, source.encode(), path.stat().st_mode & 0o7777)
        print('Updated JLab only; backup:', backup)


if __name__ == '__main__':
    main()
