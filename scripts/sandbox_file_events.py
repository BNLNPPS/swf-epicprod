"""Hand-run CLI: exact per-file event counts for a campaign's delivered
RECO files from the PanDA task sandboxes' submission CSVs, into the
events store (CAMPAIGN_DELIVERY.md, The events source; the logic is
``swf_epicprod/analytics/file_events.py``, apply_sandbox_counts).

Reads the PanDA database (task parameters), the PanDA server's sandbox
cache (the CSV and environment of each task, cached under
/data/wenauseic/swf-delivery/panda-sandboxes/) and the ANL catalog
(source totals, for the last chunk of each source). Writes ONLY the
SQLite store, and only with --apply; nothing is written to Rucio and no
data file is read. Dry by default: the same reconciliation, reported.

Run under the venv with the swf-monitor project on the path:

    cd <swf-monitor>/src && source <venv>/bin/activate && source ~/.env
    python <swf-epicprod>/scripts/sandbox_file_events.py \\
        [--campaign 26.07] [--tasks N] [--apply] [--db PATH]

Afterwards the delivery daily record picks the counts up at its next
rebuild (nightly, or scripts/backfill_delivery_history.py).
"""

import argparse
import json
import os
import sys

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402

django.setup()

from swf_epicprod.analytics.file_events import (  # noqa: E402
    DEFAULT_DB, apply_sandbox_counts)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--campaign', default='26.07',
                        help='campaign family (default 26.07)')
    parser.add_argument('--tasks', type=int, default=0,
                        help='use at most N tasks (0 = all); a check run')
    parser.add_argument('--apply', action='store_true',
                        help='write the counts to the store (dry otherwise)')
    parser.add_argument('--db', default=DEFAULT_DB)
    args = parser.parse_args()
    stats, _samples = apply_sandbox_counts(
        args.campaign, db_path=args.db, apply=args.apply, max_tasks=args.tasks)
    print(json.dumps({'campaign': args.campaign, 'apply': args.apply, **stats}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
