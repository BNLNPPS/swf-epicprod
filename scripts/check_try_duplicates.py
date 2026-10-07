"""Count manifest rows delivered more than once across a sample's tries.

A rerun writes under its try segment (RECO/<ver>/<config>/tryN/<tail>,
TAG_PREFIX), and until 2026-10-06 the residual did not credit a rerun's
files to their rows (pcs.commands._delivered_row_keys), so a later
residual could regenerate rows a rerun had already delivered. For every
tryN RECO dataset in a campaign version, this lists it and its siblings
(the base dataset at <tail> and every other try of the same tail) in
JLab Rucio, keys each file as the residual does (tail, stem, chunk),
and reports the rows present in more than one dataset with their
events. Read-only; nothing is written.

    cd <swf-monitor>/src && source ~/.env
    <venv>/bin/python <swf-epicprod>/scripts/check_try_duplicates.py [--version 26.07.1]
"""

import argparse
import os
from collections import defaultdict

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402

django.setup()

from pcs.commands import reco_row_key  # noqa: E402
from pcs.services import (_jlab_rucio_auth, _jlab_rucio_get,  # noqa: E402
                          _ndjson, fetch_jlab_rucio_did_files)


def try_datasets(root):
    """Every dataset under <root>/try*, from the JLab catalog's search."""
    token = _jlab_rucio_auth()
    found = _ndjson(_jlab_rucio_get('/dids/epic/dids/search', token,
                                    type='dataset', name=root + '/try*'))
    return sorted(n if isinstance(n, str) else n.get('name') for n in found)


def keys_of(did_name):
    """{(tail, stem, chunk): events} for one RECO dataset's files."""
    out = {}
    for f in fetch_jlab_rucio_did_files('epic', did_name):
        key = reco_row_key(str(f.get('name') or ''))
        if key is not None:
            out[key[2:]] = f.get('events')
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--version', default='26.07.1')
    parser.add_argument('--config', default='epic_craterlake')
    args = parser.parse_args()

    root = f'/RECO/{args.version}/{args.config}'
    tries = try_datasets(root)
    tails = defaultdict(set)
    for name in tries:
        rest = name[len(root) + 1:]
        _try, _, tail = rest.partition('/')
        tails[tail].add(name)
    for tail, names in sorted(tails.items()):
        base = f'{root}/{tail}'
        datasets = sorted(names) + [base]
        holders = defaultdict(list)
        sizes = {}
        for ds in datasets:
            try:
                ks = keys_of(ds)
            except Exception as exc:  # a base that does not exist is normal
                sizes[ds] = f'unreadable ({exc.__class__.__name__})'
                continue
            sizes[ds] = len(ks)
            for k, ev in ks.items():
                holders[k].append((ds, ev))
        dup = {k: v for k, v in holders.items() if len(v) > 1}
        extra = [e for v in dup.values() for _d, e in v[1:]]
        extra_events = sum(e for e in extra if e is not None)
        uncounted = sum(1 for e in extra if e is None)
        print(f'{tail}')
        for ds in datasets:
            print(f'    {sizes.get(ds)!s:>8}  {ds}')
        print(f'    rows delivered more than once: {len(dup)}'
              + (f', extra events {extra_events}'
                 + (f' plus {uncounted} copies with no recorded event count'
                    if uncounted else '') if dup else ''))


if __name__ == '__main__':
    main()
