#!/usr/bin/env python3
"""Make an Event Service canary's input dataset from EVGEN copies already
staged at BNL (swf_epicprod/evgen_es.py): a new dataset in group.EIC holding
the named staged files, ruled at BNL_PROD_DISK_1 and closed. Nothing is
copied. A canary over a file other than its manifest's first row tests that
a job is named from its own input (EPICPROD_SWITCH_ES_INPUT_NAMES).

Run in the monitor Django environment with the testbed venv and ~/.env:

    evgen-es-canary-input.py group.EIC.esinput.canary-names-20261028 <staged LFN> ...
"""
import argparse
import os

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django
django.setup()

from rucio.common.exception import DataIdentifierAlreadyExists
from swf_epicprod import evgen_es
from swf_epicprod.gke_pilots import settings as gke_settings


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('dataset', help='the new dataset name in group.EIC')
    parser.add_argument('lfns', nargs='+', help='staged copies (group.EIC.esinput.*)')
    args = parser.parse_args()
    proxy = gke_settings()['proxy']
    os.environ.update(X509_USER_PROXY=proxy, X509_CERT_DIR='/etc/grid-security/certificates')
    bnl, _ = evgen_es._catalogs(proxy)
    scope = evgen_es.BNL_SCOPE
    for name in args.lfns:
        bnl.get_metadata(scope, name)  # each must already be registered
    try:
        bnl.add_dataset(scope, args.dataset)
    except DataIdentifierAlreadyExists:
        pass
    have = {f['name'] for f in bnl.list_files(scope, args.dataset)}
    missing = [n for n in args.lfns if n not in have]
    if missing:
        bnl.attach_dids(scope, args.dataset, [{'scope': scope, 'name': n} for n in missing])
    if not list(bnl.list_did_rules(scope, args.dataset)):
        bnl.add_replication_rule([{'scope': scope, 'name': args.dataset}], 1, evgen_es.BNL_RSE)
    if bnl.get_metadata(scope, args.dataset).get('open'):
        bnl.close(scope, args.dataset)
    print(f'{scope}:{args.dataset}: {sorted(have | set(args.lfns))}')


if __name__ == '__main__':
    main()
