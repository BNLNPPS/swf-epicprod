#!/usr/bin/env python3
"""Assemble an immutable native runtime from committed code and frozen assets."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

REPO = Path(__file__).resolve().parents[1]
CODE = (
    'pilot/native/epic-native-pilot.sh',
    'pilot/epicprod-multipilot.sh',
    'pilot/native/glidein-deadline.sh',
    'pilot/native/job-user.sh',
    'pilot/native/runtime-policy.py',
    'pilot/native/queue-runtime-policy.json',
)
ASSETS = ('pilot3-native.tar.gz', 'certificates.tar.gz', 'rucio.cfg')


def stage(baseline, output, archive_sha256):
    revision = subprocess.check_output(
        ['git', 'rev-parse', 'HEAD'], cwd=REPO, text=True).strip()
    # Read every helper from that same commit, never from a previous bundle
    # or uncommitted working-tree files.
    code = {Path(path).name: subprocess.check_output(
        ['git', 'show', revision + ':' + path], cwd=REPO) for path in CODE}
    assets = {name: (baseline / name).read_bytes() for name in ASSETS}
    digest = hashlib.sha256(assets['pilot3-native.tar.gz']).hexdigest()
    if digest != archive_sha256:
        raise ValueError('pilot archive differs from the approved digest')
    output.mkdir()  # An existing version must never be overwritten.
    files = {**assets, **code}
    for name, content in files.items():
        target = output / name
        target.write_bytes(content)
        target.chmod(0o755 if name.endswith('.sh') else 0o644)
    manifest = {
        'source_commit': revision,
        'pilot_archive_sha256': digest,
        'files': {name: hashlib.sha256(content).hexdigest()
                  for name, content in sorted(files.items())},
        'code_files': sorted(code),
        'baseline_assets': sorted(assets),
    }
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--archive-sha256', required=True)
    args = parser.parse_args()
    print(json.dumps(stage(args.baseline, args.output, args.archive_sha256), indent=2))


if __name__ == '__main__':
    main()
