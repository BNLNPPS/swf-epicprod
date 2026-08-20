#!/bin/bash
# Pilot launcher loop for the BNL_NPPS_GPU queue on npps0.
#
# Decouples service lifecycle from pilot-pass lifecycle: the systemd
# service runs this loop with KillMode=process, so a service stop or
# restart kills only the loop — an in-flight pass survives orphaned and
# runs to completion. The per-GPU flock is held by the pass process
# chain, so a freshly (re)started launcher blocks until any surviving
# pass finishes before starting the next one: no double-pilot on a GPU,
# no killed payloads.
#
# The pass script is invoked fresh every cycle, so a deployed change to
# it takes effect at the next pass boundary with no service restart.
# A stuck pass is reaped by timeout(1) rather than RuntimeMaxSec (which
# would be blind to orphaned passes).

set -u

GPU="${CUDA_VISIBLE_DEVICES:-0}"
LOCK="$HOME/pilot-work/gpu${GPU}.lock"
PASS="$HOME/bin/epicprod-gpu-pilot.sh"
PASS_MAX=90000     # queue maxtime plus margin, was RuntimeMaxSec
# Between passes. Within a pass the pilot knocks every ~19s (30 requests
# over ~9.5 min observed), so the pause is the launcher's only added
# latency: keep it token. 60s here cost up to a minute of pickup latency
# for no benefit on a dedicated host.
PAUSE=5

mkdir -p "$HOME/pilot-work"

while true; do
    flock "$LOCK" timeout --kill-after=300 "$PASS_MAX" bash "$PASS"
    sleep "$PAUSE"
done
