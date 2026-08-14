#!/bin/bash
# One pilot pass on the BNL_NPPS_GPU queue (npps0 GPU server).
#
# Runs the standard BNL pilot wrapper from CVMFS in pull mode: the pilot
# asks the PanDA server for a job, runs it if one is assigned, and exits.
# The launcher loop (epicprod-gpu-pilot-launcher.sh, run by the systemd
# service) invokes this script fresh each cycle under a per-GPU flock,
# which is the whole provisioning layer for a single always-on host —
# no Harvester, no CE (README.md here; docs/VOLUNTEER_GPU_PLAN.md).
#
# Credentials: the PanDA OIDC token under $PANDA_CONFIG_ROOT and the
# Rucio x509 proxy, copies of the pandaserver02 production credentials
# (sources listed in tools/npps0/README.md).

set -u

QUEUE=BNL_NPPS_GPU
WRAPPER=/cvmfs/eic.opensciencegrid.org/panda/bnlpanda.runpilot2-wrapper.sh
WORKBASE="$HOME/pilot-work"
KEEP_RUNS=5

export PANDA_CONFIG_ROOT="$HOME/.pathena"
export X509_USER_PROXY="$HOME/creds/longproxy-for-rucio"
export EVGEN_X509_PROXY="$HOME/creds/eicprod-proxy-for-jlab"

# BNL EIC Rucio for stage-out: same environ the production queues carry
# in queuedata. RUCIO_CONFIG supplies auth_host/vo/x509 settings; the
# explicit --rucio-host below overrides the pilot's ATLAS default host
# (pilot.py falls back to config.Rucio.host when the flag is absent).
export RUCIO_CONFIG=/cvmfs/eic.opensciencegrid.org/rucio-clients/rucio.cfg
export RUCIO_ACCOUNT=panda

# S3 log stage-out (devcloud bucket): boto3 profile for the pilot's s3
# copytool (tools/npps0/config/, docs/DEVCLOUD_STAGEOUT.md).
export PANDA_PILOT_AWS_PROFILE=epic-stageout

# One GPU per pilot slot; instance 2 gets device 1 when we scale.
export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-0}"

mkdir -p "$WORKBASE"
# Retention: keep the last few pilot workdirs for debugging, drop the rest.
ls -dt "$WORKBASE"/run-* 2>/dev/null | tail -n +$((KEEP_RUNS + 1)) | xargs -r rm -rf

RUNDIR="$WORKBASE/run-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$RUNDIR"
cd "$RUNDIR"

# Git-sourced pilot configuration (tools/npps0/config/, deployed to
# ~/npps0-config): queuedata.json in the run directory makes the wrapper
# use file://$PWD/queuedata.json instead of the CRIC-derived cache; the
# storage catalog copies pre-seed the pilot info-system source files
# (LOCAL name and USER-cache names), with PILOT_HOME anchoring the
# pilot's cache directory here.
CONFDIR="$HOME/npps0-config"
export PILOT_HOME="$RUNDIR"
if [[ -f "$CONFDIR/queuedata.json" ]]; then
    cp "$CONFDIR/queuedata.json" "$RUNDIR/queuedata.json"
fi
if [[ -f "$CONFDIR/agis_ddmendpoints.json" ]]; then
    for f in agis_ddmendpoints.json \
             agis_ddmendpoints.agis.ALL.json \
             agis_ddmendpoints.agis.DEV_CLOUD_S3.json; do
        cp "$CONFDIR/agis_ddmendpoints.json" "$RUNDIR/$f"
    done
fi

exec bash "$WRAPPER" \
    -q "$QUEUE" -r "$QUEUE" -s "$QUEUE" \
    -e eic \
    -i PR -j managed \
    --pythonversion 3 --localpy \
    --pilot-user epic \
    --url https://pandaserver01.sdcc.bnl.gov -p 25443 \
    --rucio-host https://nprucio01.sdcc.bnl.gov:443 \
    --getjobrequests 30
