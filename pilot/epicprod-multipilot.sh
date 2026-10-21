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
# The wrappers start a few seconds apart: pilots of one allocation that ask
# the server for a job in the same instant contend for the same job rows,
# and the losers get no job (getJobs InFailedSqlTransaction on the server).
#
#
# Each slot draws its own time floor around the shipped one. With one floor
# for every slot, slots started together drained together and were replaced
# together, and GREX ran a third fewer jobs for an hour in every four
# (2026-10-21: about 100 slots turned over at 07:00, 11:00, 15:00 and 18:30
# UTC). A slot's floor is the shipped floor plus or minus the spread, the
# same for its wrappers so that they end together and the slot is released;
# each rotation draws again, so the slots fall further out of step.
#
# EPICPROD_PILOTS_PER_SLOT: wrappers to run (default 8).
# EPICPROD_PILOT_STAGGER: seconds between wrapper starts (default 15).
# EPICPROD_STANDARD_WRAPPER: the wrapper (default: the shipped copy, else CVMFS).
# EPICPROD_TIMEFLOOR_SPREAD: minutes either side of the shipped time floor
#   (default 30; 0 keeps it). The latest end, floor plus spread plus the
#   longest job, must stay inside the slot's wall time.

N=${EPICPROD_PILOTS_PER_SLOT:-8}
STAGGER=${EPICPROD_PILOT_STAGGER:-15}
SPREAD=${EPICPROD_TIMEFLOOR_SPREAD:-30}
TOP=$(pwd)
WRAPPER=${EPICPROD_STANDARD_WRAPPER:-${TOP}/bnlpanda.runpilot2-wrapper.sh}
[[ -r ${WRAPPER} ]] || WRAPPER=/cvmfs/eic.opensciencegrid.org/panda/bnlpanda.runpilot2-wrapper.sh

echo "$(date -u +%FT%TZ) epicprod-multipilot: ${N} wrappers (${WRAPPER}), $(nproc) cores on $(hostname)"

if [[ -r ${TOP}/queuedata.json && ${SPREAD} -gt 0 ]]; then
  base=$(sed -n 's/^ *"timefloor": *\([0-9][0-9]*\),*$/\1/p' "${TOP}/queuedata.json" | head -n 1)
  if [[ -n ${base} ]]; then
    floor=$(( base - SPREAD + RANDOM % (2 * SPREAD + 1) ))
    [[ ${floor} -lt 30 ]] && floor=30
    sed -i "s/^\( *\"timefloor\": *\)[0-9][0-9]*/\1${floor}/" "${TOP}/queuedata.json"
    echo "$(date -u +%FT%TZ) epicprod-multipilot: time floor ${floor} min (shipped ${base}, spread ${SPREAD})"
  fi
fi

pids=()
for i in $(seq 1 "${N}"); do
  mkdir -p "${TOP}/pilot-${i}"
  # A shipped queuedata.json reaches every wrapper, which reads it from its
  # working directory before the CRIC cache (OSG_SUBMISSION.md, Queue
  # behavior without a CRIC edit); a time floor there lets each pilot run
  # jobs one after another as well as side by side.
  [[ -r ${TOP}/queuedata.json ]] && cp "${TOP}/queuedata.json" "${TOP}/pilot-${i}/"
  ( cd "${TOP}/pilot-${i}" && exec bash "${WRAPPER}" "$@" ) \
    > "${TOP}/pilot-${i}.out" 2> "${TOP}/pilot-${i}.err" &
  pids+=($!)
  [[ ${i} -lt ${N} ]] && sleep "${STAGGER}"
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
