#!/bin/bash
# One job of an analysis run (docs/ANALYSIS.md, The job):
#   bash payload/analysis_run.sh <seq> <csv base>
# Sets up Rucio as run.sh does, then hands over to analysis_job.py, which
# does the work and writes analysis-report.json on every exit. Only
# analysis tasks run this; production jobs never do.
set -uo pipefail
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
seq=${1#%RNDM=}; csv_base=$2
# The submission's environment file (the doer writes environment-<base>.sh).
for f in environment-*.sh; do [ -f "$f" ] && source "$f"; done
if [ "${ANALYSIS_LOCAL:-0}" != 1 ]; then
  export RUCIO_CONFIG=${here}/rucio.cfg
  export RUCIO_ACCOUNT=${RUCIO_ACCOUNT:-eicprod}
  source "${here}/rucio_auth.sh"
  rucio_auth || echo "analysis: rucio_auth failed; the job tries the image's own client"
fi
python3 "${here}/analysis_job.py" "${seq}" "${csv_base}"
