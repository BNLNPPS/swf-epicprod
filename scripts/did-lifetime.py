#!/usr/bin/env python3
"""Give named JLab Rucio DIDs a lifetime, as a trial's outputs carry one.

For test outputs registered without a lifetime (docs/PCS.md, Trials: everything
a trial registers expires, two weeks by default). Rucio's undertaker removes
each DID when its lifetime ends, its rules with it: setting a lifetime is a
deletion, done only on Torre Wenaus's explicit approval of that deletion.
--clear removes a lifetime. Dry run by default: the
DIDs are read and their current expiry reported; --apply sets the lifetime and
reads each DID back. Runs as the production account (jlab_auth.rucio_client).

    did-lifetime.py --days 14 epic:/RECO/.../3to50deg.cES000001 ... [--apply]

The last line on stdout is a JSON summary.
"""
import argparse
import json
import sys

from swf_epicprod.jlab_auth import rucio_client


def split_did(did):
    scope, sep, name = did.partition(':')
    if not sep or not scope or not name:
        raise ValueError(f'{did!r} is not scope:name')
    return scope, name


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('dids', nargs='+', help='scope:name of each DID')
    ap.add_argument('--days', type=int, help='lifetime in days from now')
    ap.add_argument('--clear', action='store_true', help='remove the lifetime: the DIDs no longer expire')
    ap.add_argument('--apply', action='store_true', help='set it; without this nothing is written')
    args = ap.parse_args()
    if args.clear == (args.days is not None):
        sys.exit('give exactly one of --days N or --clear')
    if not args.clear and args.days < 1:
        sys.exit('--days must be at least 1')
    targets = [split_did(d) for d in args.dids]
    client = rucio_client()
    results, failed = [], 0
    for scope, name in targets:
        entry = {'did': f'{scope}:{name}'}
        try:
            entry['expired_at_before'] = str(client.get_metadata(scope, name).get('expired_at'))
            if args.apply:
                client.set_metadata(scope, name, 'lifetime',
                                    None if args.clear else args.days * 86400)
                entry['expired_at'] = str(client.get_metadata(scope, name).get('expired_at'))
        except Exception as e:                                # noqa: BLE001
            entry['error'] = str(e)
            failed += 1
        results.append(entry)
        print(json.dumps(entry), file=sys.stderr)
    print(json.dumps({'applied': args.apply, 'days': args.days, 'clear': args.clear, 'dids': len(targets),
                      'failed': failed, 'results': results}))
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
