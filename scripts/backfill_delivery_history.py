"""Hand-run CLI for the campaign delivered-data daily record
(CAMPAIGN_DELIVERY.md, Ongoing production).

The build logic lives in ``swf_epicprod/analytics/delivery_daily.py``
and runs nightly as a ``catalog_sync`` chain step; this wrapper runs
the same full idempotent reconstruction by hand. Dry-run default.

Run under the venv with the swf-monitor project on the path:

    cd <swf-monitor>/src && source <venv>/bin/activate && source ~/.env
    python <swf-epicprod>/scripts/backfill_delivery_history.py \\
        [--campaigns 26.06,26.07] [--apply]

Without --campaigns, every campaign in the catalog is covered.
"""

import argparse
import os
import sys

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')

import django  # noqa: E402

django.setup()

from swf_epicprod.analytics.delivery_daily import rebuild_delivery_daily  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--campaigns', default='',
                        help='comma-separated campaign families '
                             '(default: all in the catalog)')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--limit-files', type=int, default=0,
                        help='cap the metadata pass for fast validation '
                             '(dry-run only)')
    args = parser.parse_args()
    campaigns = tuple(c.strip() for c in args.campaigns.split(',')
                      if c.strip()) or None
    rebuild_delivery_daily(campaigns, apply=args.apply,
                           limit_files=args.limit_files)
    return 0


if __name__ == '__main__':
    sys.exit(main())
