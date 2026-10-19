#!/bin/bash
# epicprod multi-pilot launcher: N standard pilot wrappers side by side in
# one batch allocation, each pulling its own single-core job, so that the
# jobs share the allocation's memory.
#
# The batch system limits memory on the allocation as a whole. N
# single-core jobs whose total stays under the allocation run even when one
# of them peaks above the per-core share, which a single-core allocation
# would kill. This serves single-threaded payloads with uneven memory; a
# multithreaded payload pools the same way within one job.
#
# Harvester runs this as the worker's executable with the queue's pilot
# arguments, which every wrapper receives unchanged. The submit description
# sizes the allocation (N cores and N times the per-core memory) and ships
# the standard wrapper beside this script. Under harvester the wrapper works
# in its current directory and removes it at the end (get_workdir, cleanup),
# so each runs in its own directory and writes its output beside it. A stop
# signal from the batch system reaches every wrapper; the launcher exits
# when the last one has, after copying their output to its own.
#
# EPICPROD_PILOTS_PER_SLOT: wrappers to run (default 8).
# EPICPROD_STANDARD_WRAPPER: the wrapper (default: the shipped copy, else CVMFS).

N=${EPICPROD_PILOTS_PER_SLOT:-8}
TOP=$(pwd)
WRAPPER=${EPICPROD_STANDARD_WRAPPER:-${TOP}/bnlpanda.runpilot2-wrapper.sh}
[[ -r ${WRAPPER} ]] || WRAPPER=/cvmfs/eic.opensciencegrid.org/panda/bnlpanda.runpilot2-wrapper.sh

echo "$(date -u +%FT%TZ) epicprod-multipilot: ${N} wrappers (${WRAPPER}), $(nproc) cores on $(hostname)"

pids=()
for i in $(seq 1 "${N}"); do
  mkdir -p "${TOP}/pilot-${i}"
  ( cd "${TOP}/pilot-${i}" && exec bash "${WRAPPER}" "$@" ) \
    > "${TOP}/pilot-${i}.out" 2> "${TOP}/pilot-${i}.err" &
  pids+=($!)
done

pass_signal() {
  echo "$(date -u +%FT%TZ) epicprod-multipilot: signal $1, passing it to ${#pids[@]} wrappers"
  kill -s "$1" "${pids[@]}" 2>/dev/null
}
for sig in TERM INT QUIT USR1 USR2 XCPU; do
  trap "pass_signal ${sig}" "${sig}"
done

rc=0
for i in "${!pids[@]}"; do
  pid=${pids[$i]}
  # A trapped signal interrupts wait while the wrapper still runs; wait again.
  while true; do
    wait "${pid}"
    status=$?
    kill -0 "${pid}" 2>/dev/null || break
  done
  echo "$(date -u +%FT%TZ) epicprod-multipilot: pilot-$((i + 1)) exited ${status}"
  [[ ${status} -ne 0 && ${rc} -eq 0 ]] && rc=${status}
done

for i in $(seq 1 "${N}"); do
  echo "==== pilot-${i} stdout"
  cat "${TOP}/pilot-${i}.out"
  echo "==== pilot-${i} stderr" >&2
  cat "${TOP}/pilot-${i}.err" >&2
done
exit "${rc}"
