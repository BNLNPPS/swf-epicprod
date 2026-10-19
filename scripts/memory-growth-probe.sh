#!/bin/bash
# Memory growth of one production row's payload in a chosen image.
#
# Runs the payload of this checkout (swf_epicprod/payload/run.sh) inside
# IMAGE on the local host, nothing copied or registered, and prints each
# stage's peak memory and growth from the payload report (prmon). Used to
# compare the same events across images or detector versions when a
# memory signature looks like a leak (docs/SEGFAULT_DIAGNOSIS.md, memory).
#
# Usage: scripts/memory-growth-probe.sh IMAGE EVENTS SKIP EVGEN EXT [DETECTOR_VERSION]
#   IMAGE             apptainer image, e.g. /cvmfs/singularity.opensciencegrid.org/eicweb/eic_xl:nightly
#   EVENTS, SKIP      events to simulate and events to skip in the input
#   EVGEN, EXT        the row's input path under EVGEN/ and its extension (as in run.sh)
#   DETECTOR_VERSION  default 26.07.3
# Beam settings come from EBEAM and PBEAM (default 9 and 100_Au197).
# Work directories go under EPICPROD_PROBE_DIR (default /data/wenauseic/memprobe).
set -euo pipefail

image=$1 events=$2 skip=$3 evgen=$4 ext=$5 detver=${6:-26.07.3}
repo=$(cd "$(dirname "$0")/.." && pwd)
base=${EPICPROD_PROBE_DIR:-/data/wenauseic/memprobe}
mkdir -p "${base}"
work=$(mktemp -d "${base}/$(basename "${image}" | tr ':' '_').${detver}.XXXXXX")

cat > "${work}/environment-manifest.sh" <<EOF
export COPYRECO=false
export COPYFULL=false
export COPYLOG=false
export USERUCIO=false
export OUT_RSE=BNL-XRD
export LOG_RSE=EIC-XRD-LOG
export DETECTOR_VERSION=${detver}
export DETECTOR_CONFIG=epic_craterlake
export EBEAM=${EBEAM:-9}
export PBEAM=${PBEAM:-100_Au197}
export EPICPROD_SKIP_EVENTS=${skip}
EOF

echo "probe: ${image} detector ${detver}, ${events} events after ${skip}, in ${work}"
cd "${work}"
apptainer exec --bind "${work}" --bind "${repo}" "${image}" \
  bash "${repo}/swf_epicprod/payload/run.sh" "${evgen}" "${ext}" "${events}" 0 \
  > payload.out 2>&1 || echo "probe: run.sh exited $?"

python3 - "${work}/payload-report.json" <<'PY'
import json, sys
try:
    report = json.load(open(sys.argv[1]))
except (OSError, ValueError) as error:
    sys.exit(f"probe: no payload report ({error})")
stages = report.get("stages") or report.get("memory") or {}
for name, stage in stages.items():
    if not isinstance(stage, dict):
        continue
    trend = stage.get("trend", {})
    print(f"{name:22} wall {stage.get('wall_s', '?'):>6} s  peak RSS {stage.get('rss_max_kb', 0) / 1024:7.0f} MB  "
          f"start {trend.get('memory_start_kb', 0) / 1024:6.0f} MB  growth {trend.get('memory_growth_kb_per_s', 0):8.1f} kB/s")
PY
