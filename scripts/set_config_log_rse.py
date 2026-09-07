#!/usr/bin/env python
"""Give every production configuration the log store production writes to.

``log_rse`` was unset on all of them, so ``LOG_RSE`` reached the payload
empty and ``${LOG_RSE:-isLogRSE}`` resolved to a placeholder that is not a
storage element: every job spent its first log upload, and the timeout behind
it, on a certain failure. The production line PCS ingested pairs the output
and log stores as ``OUT_RSE=BNL-XRD LOG_RSE=EIC-XRD-LOG``; this sets the half
that was lost.

EIC-XRD-LOG was verified writable on 2026-09-07 from the production container
with the job's own eicprod proxy, so this is not a re-run of the 2026-08-08
refusal at that store.

Dry run by default; ``--apply`` writes. Idempotent: a configuration that
already names a log store is left alone and reported.
"""
import argparse
import os
import sys

import django

sys.path.insert(0, '/data/wenauseic/github/swf-monitor/src')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')
django.setup()

from pcs.models import ProdConfig                       # noqa: E402
from pcs.services import (STANDARD_CONFIG_LOG_RSE,      # noqa: E402
                          PLACEHOLDER_PRODCONFIG_NAME)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true',
                    help='write the change; omit for a dry run')
    ap.add_argument('--log-rse', default=STANDARD_CONFIG_LOG_RSE)
    args = ap.parse_args()

    set_, kept, skipped = [], [], []
    for cfg in ProdConfig.objects.all().order_by('name'):
        if cfg.name == PLACEHOLDER_PRODCONFIG_NAME:
            # The Placeholder anchors rows PCS adopted rather than composed
            # and carries nothing on purpose; a task bound to it takes the
            # campaign's values through the effective-config fill.
            continue
        data = dict(cfg.data or {})
        current = data.get('log_rse')
        if current == args.log_rse:
            kept.append(cfg.name)
            continue
        if current:
            # Never silently retarget a configuration that names a store.
            skipped.append((cfg.name, current))
            continue
        data['log_rse'] = args.log_rse
        set_.append(cfg.name)
        if args.apply:
            cfg.data = data
            cfg.save(update_fields=['data', 'updated_at'])

    verb = 'set' if args.apply else 'would set'
    print(f'{verb} log_rse={args.log_rse} on {len(set_)} configurations:')
    for name in set_:
        print(f'  {name}')
    if kept:
        print(f'already correct: {len(kept)}')
    for name, current in skipped:
        print(f'  LEFT ALONE (names another store): {name} -> {current}')
    if not args.apply:
        print('\ndry run — re-run with --apply to write')


if __name__ == '__main__':
    main()
