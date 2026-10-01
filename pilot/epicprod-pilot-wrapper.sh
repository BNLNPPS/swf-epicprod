#!/bin/bash
# epicprod pilot wrapper: the standard PanDA pilot wrapper, run with the
# core count of the worker it lands on and our pilot build.
#
# Published at /cvmfs/eic.opensciencegrid.org/panda/pilot/canary/
# epicprod-pilot-wrapper.sh (docs/OSG_SUBMISSION.md, Our canary pilot
# directory); a queue that runs it in place of bnlpanda.runpilot2-wrapper.sh
# passes the same arguments, which go through unchanged.
#
# The core count: ATHENA_PROC_NUMBER, which the pilot takes over the job
# definition's count to size its Event Service range fetches, and which
# the payload's node harness takes for its slots
# (NODE_EVENT_DISPATCHER.md, The core count, from the node). Kept when
# the environment already sets it; else the worker's CPU quota (a
# Kubernetes pod's CPU limit is a CFS quota, which nproc does not see);
# else the cores the process may run on. No queue declaration is read.
#
# The pilot: our build in the same directory (pilot3.tar.gz, the
# symlink naming the one offered), appended as --piloturl so it wins
# over any given earlier; EPICPROD_PILOTURL overrides it.

HERE=/cvmfs/eic.opensciencegrid.org/panda/pilot/canary
STANDARD_WRAPPER=${EPICPROD_STANDARD_WRAPPER:-/cvmfs/eic.opensciencegrid.org/panda/bnlpanda.runpilot2-wrapper.sh}
PILOTURL=${EPICPROD_PILOTURL:-file://${HERE}/pilot3.tar.gz}
CGROUP=${EPICPROD_CGROUP_ROOT:-/sys/fs/cgroup}

worker_cores() {
  local quota period
  if [[ -r ${CGROUP}/cpu.max ]]; then                      # cgroup v2
    read -r quota period < ${CGROUP}/cpu.max
    if [[ ${quota} != max && ${period:-0} -gt 0 ]]; then
      echo $(( quota / period > 0 ? quota / period : 1 ))
      return
    fi
  elif [[ -r ${CGROUP}/cpu/cpu.cfs_quota_us ]]; then       # cgroup v1
    quota=$(cat ${CGROUP}/cpu/cpu.cfs_quota_us)
    period=$(cat ${CGROUP}/cpu/cpu.cfs_period_us 2>/dev/null)
    if [[ ${quota} -gt 0 && ${period:-0} -gt 0 ]]; then
      echo $(( quota / period > 0 ? quota / period : 1 ))
      return
    fi
  fi
  nproc
}

if [[ -z ${ATHENA_PROC_NUMBER:-} || ${ATHENA_PROC_NUMBER} -le 0 ]]; then
  export ATHENA_PROC_NUMBER=$(worker_cores)
  source_note="from the worker"
else
  source_note="from the environment"
fi
echo "epicprod-pilot-wrapper: ATHENA_PROC_NUMBER=${ATHENA_PROC_NUMBER} (${source_note}), piloturl ${PILOTURL}"

# The pilot environment our Event Service queues run with, as the npps0
# pass script and the Perlmutter launch set it (NODE_EVENT_DISPATCHER.md):
# BNL Rucio for stage-out, the generic executor, and yampl (the channel it
# hands ranges through), built for the pilot's Python 3.11, from this
# directory. A value already set is kept.
export RUCIO_CONFIG=${RUCIO_CONFIG:-/cvmfs/eic.opensciencegrid.org/rucio-clients/rucio.cfg}
export RUCIO_ACCOUNT=${RUCIO_ACCOUNT:-panda}
export PILOT_ES_EXECUTOR_TYPE=${PILOT_ES_EXECUTOR_TYPE:-generic}
if [[ -d ${HERE}/es-channel-py311-el9/python ]]; then
  export PYTHONPATH="${HERE}/es-channel-py311-el9/python${PYTHONPATH:+:$PYTHONPATH}"
fi

# The queue's pilot-side configuration, when one is published here
# (queuedata/<queue>.json: the server's, with the es_events activities):
# the standard wrapper reads queuedata.json from its working directory.
queue=""
args=("$@")
for ((i = 0; i < ${#args[@]}; i++)); do
  [[ ${args[i]} == -q ]] && queue=${args[i+1]:-}
done
if [[ -n ${queue} && -f ${HERE}/queuedata/${queue}.json && ! -f queuedata.json ]]; then
  cp "${HERE}/queuedata/${queue}.json" queuedata.json
  echo "epicprod-pilot-wrapper: queuedata.json from ${HERE}/queuedata/${queue}.json"
fi

# The standard wrapper is not executable on CVMFS (mode 644); bash runs it,
# as Harvester's pods do. Our servers and pilot flavour come first, so the
# caller's arguments can override them; our pilot comes last, so it wins.
# --allow-same-user=False, as the Perlmutter launch passes: with it on, a
# pilot's request after its first job carries that job's task as a string
# task_id, which the server's v1 API refuses (GKE pod, 2026-10-01).
exec bash "${STANDARD_WRAPPER}" \
  -e eic --pythonversion 3 --pilot-user epic --allow-same-user=False \
  --url https://pandaserver01.sdcc.bnl.gov -p 25443 \
  --rucio-host https://nprucio01.sdcc.bnl.gov:443 \
  "$@" --piloturl "${PILOTURL}"
