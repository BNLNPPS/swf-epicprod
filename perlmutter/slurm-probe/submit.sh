#!/bin/bash
# Submit the Slurm allocation probe (probe.py) as a one-job canary task
# against a Perlmutter queue, through the same submission kernel as the
# site canary's probe. The job's record arrives as PanDA job metadata
# (jobReport.json) and between SLURM-PROBE markers in its stdout.
#
# Usage: bash submit.sh [queue]
# Requires the cached panda-client OIDC token (~/pclient/run/setup.sh).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
RELEASE="${SWF_MONITOR_RELEASE:-/opt/swf-monitor/current}"
QUEUE="${1:-NERSC_Perlmutter_epic}"
QTAG="$(echo "${QUEUE}" | tr 'A-Z' 'a-z')"
STAMP="$(date +%Y%m%d%H%M%S)"
WORK="${SWF_TMP_DIR:-/data/swf-tmp}/slurm-probe/${STAMP}"
mkdir -p "${WORK}/sandbox"
cp "${HERE}/probe.py" "${WORK}/sandbox/"
cat > "${WORK}/spec.json" <<EOF
{
  "outDS": "group.EIC.canary.slurmprobe.${QTAG}.${STAMP}",
  "exec": "python3 probe.py",
  "site": "${QUEUE}",
  "prodSourceLabel": "test",
  "processingType": "canary",
  "userName": "canary",
  "nJobs": 1,
  "nCore": 1,
  "memory": 2048,
  "walltimeHours": 0.084,
  "skipScout": true,
  "maxAttempt": 1,
  "containerImage": "/cvmfs/singularity.opensciencegrid.org/eicweb/eic_xl:26.07.0-stable"
}
EOF
echo "spec: ${WORK}/spec.json" >&2
source ~/pclient/run/setup.sh
export PANDA_AUTH_VO=EIC.production
python3 "${RELEASE}/scripts/evgen_panda_submit.py" \
    --spec "${WORK}/spec.json" --workdir "${WORK}/sandbox"
