#!/usr/bin/env python3
"""Assemble an immutable native runtime from committed code and frozen assets."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile

REPO = Path(__file__).resolve().parents[1]
CODE = (
    'pilot/native/epic-native-pilot.sh',
    'pilot/epicprod-multipilot.sh',
    'pilot/native/glidein-deadline.sh',
    'pilot/native/job-user.sh',
    'pilot/native/runtime-policy.py',
    'pilot/native/queue-runtime-policy.json',
)
ASSETS = ('pilot3-native.tar.gz', 'certificates.tar.gz', 'rucio.cfg',
          'queuedata-source.json')
# A pilot built with the bounded ES batch channel (build-native-es-batch.py)
# refuses ES stage-out without its gate, so the manifest declares it and the
# launching site exports the gate exactly when its runtime carries it.
ES_BATCH_MODULE = 'pilot3/pilot/user/epic/es_batch.py'
ES_BATCH_STAGEOUT_S = 300
# The channel's bounds as es_batch.py sets them; the Perlmutter launcher
# admits a runtime only with this declaration (version 1).
ES_BATCH_CHANNEL = {'version': 1, 'stageout_s': ES_BATCH_STAGEOUT_S, 'max_ranges_per_ipc': 512,
                    'min_acquisition_spacing_s': 2, 'job_identity': 'JobData.jobid'}


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
    queuedata = json.loads(assets['queuedata-source.json'])
    if not isinstance(queuedata, dict) or not (queuedata.get('panda_queue') or queuedata.get('nickname')):
        raise ValueError('queuedata-source.json must identify its PanDA queue')
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
    with tarfile.open(fileobj=io.BytesIO(assets['pilot3-native.tar.gz'])) as archive:
        if ES_BATCH_MODULE in archive.getnames():
            manifest['es_batch_channel'] = dict(ES_BATCH_CHANNEL)
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return manifest


def tarball(directory):
    """The runtime as launchers fetch it, ``<directory>.tar.gz`` beside it:
    its files flat, owned by root, as the published runtimes are. An existing
    archive is never overwritten."""
    path = directory.parent / (directory.name + '.tar.gz')
    with path.open('xb') as raw, tarfile.open(fileobj=raw, mode='w:gz') as archive:
        for target in sorted(directory.iterdir()):
            info = archive.gettarinfo(str(target), arcname=target.name)
            info.uid = info.gid = 0
            info.uname = info.gname = 'root'
            with target.open('rb') as content:
                archive.addfile(info, content)
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--archive-sha256', required=True)
    parser.add_argument('--tarball', action='store_true',
                        help='also write <output>.tar.gz, the form publish-pilot-asset.py publishes')
    args = parser.parse_args()
    print(json.dumps(stage(args.baseline, args.output, args.archive_sha256), indent=2))
    if args.tarball:
        path = tarball(args.output)
        print(json.dumps({'tarball': str(path),
                          'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}, indent=2))


if __name__ == '__main__':
    main()
