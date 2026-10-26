#!/usr/bin/env python3
"""Publish a pilot asset (a native runtime bundle, a channel archive) under
the devcloud bucket's public ``pilot/`` prefix, which launchers fetch by a
checksum-pinned URL (docs/NERSC_PERLMUTTER.md, Controlling the pilot).

The object is immutable: an existing key is never overwritten. After the
upload the public URL is read back and its SHA-256 compared with the local
file; the result is written beside the file as ``<name>.published.json``.
Uses the AWS profile ``epic-stageout``.

    publish-pilot-asset.py /data/swf-tmp/native-runtime-perlmutter-es-batch-<commit>.tar.gz
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import urllib.request

BUCKET = 'epic-devcloud-stageout'
PUBLIC = 'https://epic-devcloud-stageout.s3.us-east-1.amazonaws.com/'


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('file', type=Path)
    ap.add_argument('--profile', default='epic-stageout')
    args = ap.parse_args()
    import boto3
    data = args.file.read_bytes()
    local = hashlib.sha256(data).hexdigest()
    key = 'pilot/' + args.file.name
    client = boto3.Session(profile_name=args.profile).client('s3', region_name='us-east-1')
    client.put_object(Bucket=BUCKET, Key=key, Body=data, IfNoneMatch='*',
                      ContentType='application/gzip' if args.file.name.endswith('.gz') else 'application/octet-stream')
    url = PUBLIC + key
    with urllib.request.urlopen(url, timeout=60) as response:
        served = hashlib.sha256(response.read()).hexdigest()
    if served != local:
        sys.exit(f'ERROR: {url} serves {served}, not the published {local}')
    result = {'url': url, 'sha256': local, 'bytes': len(data)}
    Path(str(args.file) + '.published.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
