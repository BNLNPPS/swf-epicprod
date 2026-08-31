#!/bin/bash
# Nightly campaign-assembly generation (docs/CONTINUOUS_PRODUCTION.md,
# Campaign assembly): regenerates the standing proposal set so
# configurations added via PCS appear on the plan page. Unchanged
# recommendations are no-ops (pending proposals keep their refs);
# decided entries are never altered. Runs from the wenauseic crontab
# on swf-testbed at 03:15 ET.
set -euo pipefail
source ~/.env
cd /data/wenauseic/github/swf-monitor/src
exec /data/wenauseic/github/swf-testbed/.venv/bin/python \
    /data/wenauseic/github/swf-epicprod/scripts/propose-campaign-plan.py \
    --source 26.07 --target 26.09 --apply --created-by nightly_cron
