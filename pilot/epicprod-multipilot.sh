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
# Each allocation draws its own time floor around the shipped one. With one floor
# for every slot, slots started together drained together and were replaced
# together, and GREX ran a third fewer jobs for an hour in every four
# (2026-10-21: about 100 slots turned over at 07:00, 11:00, 15:00 and 18:30
# UTC). A slot's floor is the shipped floor plus or minus the spread, the
# same for its wrappers so that they end together and the slot is released;
# each rotation draws again, so the slots fall further out of step. This
# spreads retirements; it does not remove the final partially occupied tail.
# The useful lifetime must be sized for the current job durations.
#
# EPICPROD_PILOTS_PER_SLOT: wrappers to run (default 8).
# EPICPROD_PILOT_STAGGER: seconds between wrapper starts (default 15).
# EPICPROD_STANDARD_WRAPPER: the wrapper (default: the shipped copy, else CVMFS).
# EPICPROD_TIMEFLOOR_SPREAD: minutes either side of the shipped time floor
#   (default 30; 0 keeps it).
# EPICPROD_ALLOCATION_SECONDS: actual batch walltime (legacy default 18000).
# EPICPROD_DRAIN_RESERVE_SECONDS: time kept for the last job (default 7200).
# EPICPROD_STARTUP_MARGIN_SECONDS: allowance for wrapper setup (default 300).
# The submit description supplies the allocation and reserve together.
# Every child receives the remaining budget to ONE allocation deadline;
# starting later must not reset the clock. Missing queue data is an error.

N=${EPICPROD_PILOTS_PER_SLOT:-8}
STAGGER=${EPICPROD_PILOT_STAGGER:-15}
SPREAD=${EPICPROD_TIMEFLOOR_SPREAD:-30}
ALLOCATION=${EPICPROD_ALLOCATION_SECONDS:-18000}
RESERVE=${EPICPROD_DRAIN_RESERVE_SECONDS:-7200}
MARGIN=${EPICPROD_STARTUP_MARGIN_SECONDS:-300}
TOP=$(pwd)
if [[ -n ${SLURM_JOB_ID:-} && $TOP == /var/lib/condor-ce/* ]]; then
  # The CE sandbox is a small shared spool, not worker storage. Keep pilot
  # extraction, job directories and logs on the batch worker's filesystem.
  export EPICPROD_CE_SANDBOX=$TOP
  worker_tmp=${SLURM_TMPDIR:-/tmp}
  [[ -d $worker_tmp && -w $worker_tmp ]] || { echo 'No writable worker temporary directory' >&2; exit 66; }
  [[ $(stat -c %d "$worker_tmp") != $(stat -c %d "$TOP") ]] || {
    echo 'Worker temporary directory is on the CE spool; refusing to launch' >&2; exit 66; }
  TOP=$(mktemp -d "$worker_tmp/epicprod-pilots.${SLURM_JOB_ID}.XXXXXX") || exit 66
  for name in epic-native-pilot.sh pilot3-native.tar.gz certificates.tar.gz rucio.cfg \
              glidein-deadline.sh job-user.sh runtime-policy.py queue-runtime-policy.json \
              queuedata.json x509up_u1000; do
    [[ ! -f $EPICPROD_CE_SANDBOX/$name ]] || cp "$EPICPROD_CE_SANDBOX/$name" "$TOP/$name" || exit 66
  done
  chmod 600 "$TOP/x509up_u1000" 2>/dev/null || true
  cd "$TOP" || exit 66
  export _CONDOR_SCRATCH_DIR=$TOP EPICPROD_WORKER_SCRATCH=$TOP
  echo "epicprod-multipilot: worker directory $TOP; CE sandbox $EPICPROD_CE_SANDBOX"
fi
WRAPPER=${EPICPROD_STANDARD_WRAPPER:-${TOP}/bnlpanda.runpilot2-wrapper.sh}
[[ -r ${WRAPPER} ]] || WRAPPER=/cvmfs/eic.opensciencegrid.org/panda/bnlpanda.runpilot2-wrapper.sh
# Each wrapper runs in its own subdirectory: the path must be absolute, and the
# native launcher finds the allocation's shipped files here (epic-native-pilot.sh).
WRAPPER=$(readlink -f "${WRAPPER}")
export EPICPROD_NATIVE_ASSETS=${EPICPROD_NATIVE_ASSETS:-${TOP}}

for value in "$N" "$STAGGER" "$SPREAD" "$ALLOCATION" "$RESERVE" "$MARGIN"; do
  if [[ ! $value =~ ^(0|[1-9][0-9]*)$ ]]; then
    echo "epicprod-multipilot: invalid integer policy value: $value" >&2
    exit 64
  fi
done
if (( N < 1 || ALLOCATION <= RESERVE + MARGIN || RESERVE < 1 )); then
  echo "epicprod-multipilot: allocation has no useful job-intake window" >&2
  exit 64
fi
if [[ ! -r ${TOP}/queuedata.json || ! -r $WRAPPER ]]; then
  echo "epicprod-multipilot: missing queuedata.json or standard wrapper" >&2
  exit 64
fi
base=$(sed -n 's/^ *"timefloor": *\([0-9][0-9]*\),*$/\1/p' "${TOP}/queuedata.json")
shipped_max=$(sed -n 's/^ *"maxtime": *\([0-9][0-9]*\),*$/\1/p' "${TOP}/queuedata.json")
if [[ ! $base =~ ^[1-9][0-9]*$ || ! $shipped_max =~ ^[1-9][0-9]*$ ]]; then
  echo "epicprod-multipilot: queuedata requires positive integer timefloor and maxtime, one field per line" >&2
  exit 64
fi
(( shipped_max < ALLOCATION )) && ALLOCATION=$shipped_max
if (( ALLOCATION <= RESERVE + MARGIN + (N - 1) * STAGGER + 60 )); then
  echo "epicprod-multipilot: queue/batch budget is too short to start all pilots safely" >&2
  exit 64
fi
floor=$(( base - SPREAD + RANDOM % (2 * SPREAD + 1) ))
(( floor < 1 )) && floor=1
max_floor=$(( (ALLOCATION - RESERVE - MARGIN) / 60 ))
(( floor > max_floor )) && floor=$max_floor
started=$(date +%s)
deadline=$(( started + ALLOCATION - MARGIN ))
intake_deadline=$(( started + floor * 60 ))

echo "$(date -u +%FT%TZ) epicprod-multipilot: launcher=deadline-v2 policy=${EPICPROD_LIFETIME_POLICY:-legacy} ${N} wrappers (${WRAPPER}), $(nproc) cores on $(hostname)"
echo "$(date -u +%FT%TZ) epicprod-multipilot: time floor ${floor} min (shipped ${base}, spread ${SPREAD}); allocation=${ALLOCATION}s reserve=${RESERVE}s startup_margin=${MARGIN}s deadline=${deadline} intake_deadline=${intake_deadline}"

pids=()
launch_rc=0
for i in $(seq 1 "${N}"); do
  mkdir -p "${TOP}/pilot-${i}"
  # A shipped queuedata.json reaches every wrapper, which reads it from its
  # working directory before the CRIC cache (OSG_SUBMISSION.md, Queue
  # behavior without a CRIC edit); a time floor there lets each pilot run
  # jobs one after another as well as side by side.
  now=$(date +%s)
  remaining=$(( deadline - now ))
  child_floor=$(( (intake_deadline - now) / 60 ))
  if (( child_floor < 1 || remaining <= RESERVE )); then
    echo "epicprod-multipilot: no time left to start pilot-${i}" >&2
    launch_rc=1
    break
  fi
  sed -e "s/^\( *\"timefloor\": *\)[0-9][0-9]*/\1${child_floor}/" \
      -e "s/^\( *\"maxtime\": *\)[0-9][0-9]*/\1${remaining}/" \
      "${TOP}/queuedata.json" > "${TOP}/pilot-${i}/queuedata.json"
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

rc=$launch_rc
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
