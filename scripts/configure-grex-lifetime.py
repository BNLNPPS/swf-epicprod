#!/usr/bin/env python3
"""Install GREX's allocation budget and matching launcher; plan unless --apply.

Run on pandaharvester01 from a checkout of this repository. Harvester reads
the submit template on each submission, so no daemon restart is needed.
Install this before publishing the longer-lived schedconfig overrides.
"""
import argparse
import datetime as dt
import os
from pathlib import Path
import re
import shutil
import tempfile


POLICY = {
    'EPICPROD_ALLOCATION_SECONDS': '43200',
    'EPICPROD_DRAIN_RESERVE_SECONDS': '10800',
    'EPICPROD_LIFETIME_POLICY': 'grex-12h-20261022',
}


def submit_template(text):
    required = {
        'executable': r'^executable\s*=\s*\S*/epicprod-multipilot\.sh\s*$',
        'eight cores': r'^\+xcount\s*=\s*8\s*$',
        '48000 MB': r'^\+maxMemory\s*=\s*48000\s*$',
        'GREX queue data': r'^transfer_input_files\s*=.*queuedata/UM_GREX_PanDA_1/queuedata\.json\s*$',
    }
    for label, pattern in required.items():
        if len(re.findall(pattern, text, re.MULTILINE)) != 1:
            raise ValueError(f'unexpected submit template: {label}')
    match = re.findall(r'^\+maxWallTime\s*=\s*(\d+)\s*$', text, re.MULTILINE)
    if match not in (['300'], ['720']):
        raise ValueError(f'unexpected allocation walltime: {match}')
    text = re.sub(r'^\+maxWallTime\s*=\s*\d+\s*$', '+maxWallTime = 720', text, flags=re.MULTILINE)
    pattern = r'^(environment\s*=\s*")([^\n"]*)("\s*)$'
    matches = list(re.finditer(pattern, text, re.MULTILINE))
    if len(matches) != 1:
        raise ValueError('expected one quoted environment line')
    env = matches[0].group(2)
    for key, value in POLICY.items():
        env = re.sub(r'(?:^|\s)' + key + r'=\S+', '', env)
        env += f' {key}={value}'
    return re.sub(pattern, lambda m: m.group(1) + env.strip() + m.group(3), text, flags=re.MULTILINE)


def install(path, contents, stamp):
    original = path.read_bytes()
    if contents == original:
        print(f'unchanged {path}')
        return
    st = path.stat()
    backup = path.with_name(path.name + '.before-grex-lifetime-' + stamp)
    shutil.copy2(path, backup)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name + '.')
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, st.st_mode & 0o7777)
        os.chown(temporary, st.st_uid, st.st_gid)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print(f'updated {path}; backup {backup}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--common', type=Path, default=Path('/data/atlpan/harvester_common'))
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1] / 'pilot' / 'epicprod-multipilot.sh'
    launcher = args.common / 'epicprod-multipilot.sh'
    template = args.common / 'submit_pilot3_pull_umgrex.sdf'
    for path in (launcher, template):
        if path.is_symlink() or not path.is_file():
            raise ValueError(f'expected a regular installed file: {path}')
    # Validate both inputs before any write. Launcher first makes a mixed
    # rollout safe: it clamps queue data to the batch budget, even if older.
    new_launcher = source.read_bytes()
    if b'launcher=deadline-v2' not in new_launcher:
        raise ValueError('source launcher does not enforce the allocation deadline')
    new_template = submit_template(template.read_text()).encode()
    print('GREX: 8 cores, 48000 MB, 720 minutes; reserve 180 minutes; launcher deadline-v2')
    if args.apply:
        stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
        install(launcher, new_launcher, stamp)
        install(template, new_template, stamp)
    else:
        print('plan only; use --apply to install')


if __name__ == '__main__':
    main()
