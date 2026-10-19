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
# The image carries its own CA store; host certificate settings point outside it.
unset SSL_CERT_FILE CURL_CA_BUNDLE REQUESTS_CA_BUNDLE
apptainer exec --bind "${work}" --bind "${repo}" "${image}" \
  bash "${repo}/swf_epicprod/payload/run.sh" "${evgen}" "${ext}" "${events}" 0 \
  > payload.out 2>&1 &
payload=$!
# Simulation memory sampled from outside: the payload keeps its prmon series
# only on the grid. The npsim process RSS every 5 s while the payload runs.
while kill -0 "${payload}" 2>/dev/null; do
  rss=$(ps -eo rss=,args= | awk '/[n]psim|[d]dsim/ && !/awk/ {s += $1} END {print s + 0}')
  echo "$(date +%s) ${rss}" >> npsim-rss.txt
  sleep 5
done
wait "${payload}" || echo "probe: run.sh exited $?"

python3 - "${work}/npsim-rss.txt" "${events}" <<'PY'
import sys
events = int(sys.argv[2])
rows = [tuple(map(int, line.split())) for line in open(sys.argv[1]) if line.split()]
rows = [(t, kb) for t, kb in rows if kb > 0]
if len(rows) < 4:
    sys.exit("probe: npsim was not seen running")
t0 = rows[0][0]
xs = [t - t0 for t, _ in rows]
ys = [kb / 1024 for _, kb in rows]
# Growth over the second half of the run, past geometry and physics setup.
half = [(x, y) for x, y in zip(xs, ys) if x >= xs[-1] / 2]
n = len(half)
mx = sum(x for x, _ in half) / n
my = sum(y for _, y in half) / n
slope = sum((x - mx) * (y - my) for x, y in half) / max(sum((x - mx) ** 2 for x, _ in half), 1e-9)
print(f"simulation: {xs[-1]} s, {events} events; RSS at 60 s {next(y for x, y in zip(xs, ys) if x >= min(60, xs[-1])):.0f} MB, "
      f"peak {max(ys):.0f} MB, end {ys[-1]:.0f} MB; growth over the second half {slope * 60:.1f} MB/min, "
      f"about {slope * xs[-1] / events:.0f} MB per event")
PY
