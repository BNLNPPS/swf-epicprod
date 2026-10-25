#!/usr/bin/env python3
"""Fast PODIO merge with the first file's compression and metadata policy."""
import argparse
from pathlib import Path

from podio_root_merge import merge_files


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--inputs-file', required=True, type=Path)
    ap.add_argument('--output', required=True)
    args = ap.parse_args()
    files = args.inputs_file.read_text().splitlines()
    if not files or any(not name for name in files) or len(files) != len(set(files)):
        ap.error('the input manifest must contain distinct, nonempty file paths')
    merge_files(files, args.output, metadata='first', compression=0)


if __name__ == '__main__':
    main()
