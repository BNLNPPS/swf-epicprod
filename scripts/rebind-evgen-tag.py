#!/usr/bin/env python3
"""rebind-evgen-tag.py — replace an evgen tag the sources do not name.

The binding rule (docs/PCS.md, Datasets) gives an edition the evgen tag its
source names. A tag minted from a misread source carries neither the
generator nor the version of what it is bound to: e55 (``pythia6 6-eic``)
holds the 26.07.1 editions of two pythia6-eic releases, 1.0.0 and 1.3.0.
This runs ``pcs.services.rebind_anchor_editions`` scoped to that tag: each
of the family's editions on it takes the evgen tag its source supports,
found or created by ``find_or_create_evgen_tag``. The rebind renames each
edition (the former name stays resolvable from ``metadata['rebind']``) and
leaves its physics configuration, source, DIDs, targets and task links in
place. With ``--retire``, the tag is retired once no edition carries it
(``pcs.permanence.set_lifecycle``; the tag itself stays, as every issued
identity does).

The run is one transaction under the actor and reason in the identity
history, with one ``edition_rebind`` action record. Dry run by default;
``--apply`` writes.

    cd /data/wenauseic/github/swf-monitor/src
    ../../swf-testbed/.venv/bin/python ../../swf-epicprod/scripts/rebind-evgen-tag.py \\
        --family 26.07 --evgen-tag e55 --by NAME [--retire] [--apply]
"""
import argparse
import json
import os
import sys

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.environ.get('SWF_MONITOR_SRC',
                                  os.path.join(THIS_DIR, '..', '..', 'swf-monitor', 'src')))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402
django.setup()

from django.db import transaction  # noqa: E402

from pcs.models import Dataset, EvgenTag  # noqa: E402
from pcs.permanence import identity_change, set_lifecycle  # noqa: E402
from pcs.services import rebind_anchor_editions  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--family', required=True, help='Campaign family, e.g. 26.07')
    ap.add_argument('--evgen-tag', required=True, help='The tag to replace, e.g. e55')
    ap.add_argument('--by', required=True, help='The acting person')
    ap.add_argument('--retire', action='store_true',
                    help='Retire the tag when no edition carries it after the rebind')
    ap.add_argument('--apply', action='store_true', help='Write. Without it nothing is written.')
    args = ap.parse_args()

    tag = EvgenTag.objects.get(tag_number=int(args.evgen_tag.lower().removeprefix('e')))
    reason = f'evgen tag {tag.tag_label} replaced by the binding rule'
    if not args.apply:
        report = rebind_anchor_editions(args.family, changed_by=args.by, dry_run=True,
                                        evgen_tag=tag.tag_label)
        print(json.dumps(report, indent=1))
        print('Dry run: nothing written.')
        return 0
    with transaction.atomic(), identity_change(args.by, reason):
        report = rebind_anchor_editions(args.family, changed_by=args.by, dry_run=False,
                                        evgen_tag=tag.tag_label)
        left = Dataset.objects.filter(evgen_tag=tag).count()
        report['left_on_tag'] = left
        if args.retire:
            if left:
                raise SystemExit(f'{tag.tag_label}: {left} editions still carry it; '
                                 'not retired, nothing written')
            set_lifecycle(tag, 'retired', changed_by=args.by,
                          reason=f'{reason}; its editions renamed: '
                                 f'{len(report["renamed"])}')
            report['retired'] = tag.tag_label
    print(json.dumps(report, indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(main())
