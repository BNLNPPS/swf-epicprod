"""Hand-run CLI for per-file event measurement of delivered campaign
data (CAMPAIGN_DELIVERY.md, The events source).

The measurement logic lives in ``swf_epicprod/analytics/file_events.py``
and runs nightly as a ``catalog_sync`` chain step; this wrapper runs the
same incremental pass by hand. Disk replicas only — never tape.

Run under the venv with the swf-monitor project on the path:

    cd <swf-monitor>/src && source <venv>/bin/activate && source ~/.env
    python <swf-epicprod>/scripts/measure_file_events.py \\
        [--campaigns 26.06,26.07] [--locations N] [--workers 6]

The store defaults to /data/wenauseic/swf-delivery/file_events.sqlite.
"""

import argparse
import os
import sys

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402

django.setup()

from swf_epicprod.analytics.file_events import (  # noqa: E402
    DEFAULT_DB, measure_file_events)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--campaigns', default='',
                        help='comma-separated campaign families '
                             '(default: all in the catalog)')
    parser.add_argument('--db', default=DEFAULT_DB)
    parser.add_argument('--workers', type=int, default=6)
    parser.add_argument('--locations', type=int, default=0,
                        help='process at most N locations (0 = all)')
    args = parser.parse_args()
    campaigns = [c.strip() for c in args.campaigns.split(',') if c.strip()]
    measure_file_events(campaigns, db_path=args.db, workers=args.workers,
                        max_locations=args.locations)
    return 0


if __name__ == '__main__':
    sys.exit(main())
