#!/bin/bash
# One job of an analysis run (docs/ANALYSIS.md, The job):
#   bash payload/analysis_run.sh <n> <csv base>
# <n> is PanDA's ${SEQNUMBER} for the job, which counts from 1. A local run
# passes the exec unsubstituted (%RNDM=0), read as JEDI would: offset + 1.
# Sets up Rucio as run.sh does, then hands over to analysis_job.py, which
# does the work and writes analysis-report.json on every exit. Only
# analysis tasks run this; production jobs never do.
set -uo pipefail
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
case $1 in %RNDM=*) n=$(( ${1#%RNDM=} + 1 )) ;; *) n=$1 ;; esac; csv_base=$2
# The submission's environment file (the doer writes environment-<base>.sh).
for f in environment-*.sh; do [ -f "$f" ] && source "$f"; done
if [ "${ANALYSIS_LOCAL:-0}" != 1 ]; then
  export RUCIO_CONFIG=${here}/rucio.cfg
  # eicprod always, as run.sh: the pilot's environment names its own
  # account (panda), which the job's credentials do not sign in to.
  export RUCIO_ACCOUNT=eicprod
  source "${here}/rucio_auth.sh"
  rucio_auth || echo "analysis: rucio_auth failed; the job tries the image's own client"
fi
python3 "${here}/analysis_job.py" "${n}" "${csv_base}"
