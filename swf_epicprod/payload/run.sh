#!/bin/bash
set -Euo pipefail
# Stage log: one line per stage start, end and failure, in the job working
# directory (PAYLOAD_STAGES_LOG), read by the epicprod dispatcher for the
# payload report and the canary verdict (swf-epicprod docs/EPICPROD_PAYLOAD.md).
CURRENT_STAGE=""
stage() {
  CURRENT_STAGE=$1
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $1 $2${3:+ $3}" >> "${PAYLOAD_STAGES_LOG:-payload-stages.log}"
  # As each stage starts and ends, refresh the report, so the metrics the
  # pilot sends on its next heartbeat say where the payload has got to. A
  # job that dies with its worker, or is killed at the wall inside a long
  # stage, has then already reported the stage it was in and what it had
  # done, which the job record keeps; its metadata, and so its full
  # report, would not survive. The refresh never fails the payload.
  if [ -n "${TASKNAME:-}" ]; then
    payload_report "" --quiet || true
  fi
}
# Every stage that runs a program runs under its own prmon, named for the
# stage, so wall time, CPU, memory and I/O are attributed to the stage
# rather than to the job as a whole; the pilot measures the job, this
# measures its parts. prmon exits with the exit code of the program it
# watches, so wrapping a stage leaves its failure handling unchanged.
# Without prmon on the path the program simply runs unwatched.
monitor() {
  local label=$1; shift
  if command -v prmon >/dev/null 2>&1 && [ -n "${LOG_TEMP:-}" ] && [ -d "${LOG_TEMP}" ]; then
    prmon --filename "${LOG_TEMP}/${TASKNAME}.${label}.prmon.txt" \
          --json-summary "${LOG_TEMP}/${TASKNAME}.${label}.prmon.json" \
          --log-filename "${LOG_TEMP}/${TASKNAME}.${label}.prmon.log" \
          --interval "${PRMON_INTERVAL:-1}" \
          -- "$@"
  else
    "$@"
  fi
}
# A crash (a stage's program dead on a signal: exit 128 + N) leaves its
# evidence in the stage's log, which never reaches anyone when the job
# dies before the Logs stage. So the trap, before exiting on a crash-class
# status, puts the tail of the failing stage's log into the report's note
# (the report goes out on the EXIT trap, through the report channel) and
# runs the log upload, so the stage logs of a crashed job reach LOG_RSE
# as a finished job's do (swf-epicprod docs/SEGFAULT_DIAGNOSIS.md, Traces
# going forward). Neither changes the exit code.
CRASH_TAIL_LINES=200
crash_capture() {
  local s=$1
  trap - ERR
  local program
  case "${CURRENT_STAGE}" in
    simulation) program=npsim ;;
    reconstruction) program=eicrecon ;;
    background) program=hepmcmerger ;;
    evgen) program=evgen ;;
    *) program="" ;;
  esac
  local stage_log="${LOG_TEMP:-}/${TASKNAME:-}.${program}.log"
  if [ -n "${program}" ] && [ -f "${stage_log}" ]; then
    REPORT_NOTE="crash: ${CURRENT_STAGE} (${program}) exited ${s} (signal $((s - 128))); last ${CRASH_TAIL_LINES} lines of ${program}.log:"$'\n'"$(tail -n "${CRASH_TAIL_LINES}" "${stage_log}" 2>/dev/null || true)"
  else
    REPORT_NOTE="crash: ${CURRENT_STAGE:-unknown stage} exited ${s} (signal $((s - 128))); no stage log to read"
  fi
  echo "crash capture: ${CURRENT_STAGE:-?} exited ${s}; the report carries the stage log tail"
  if [ "${COPYLOG:-false}" == "true" ] && [ "${USERUCIO:-false}" == "true" ] && [ -n "${LOG_TEMP:-}" ]; then
    upload_logs || true
  fi
}
trap 's=$?; echo "$0: Error on line "$LINENO": $BASH_COMMAND"; if [ -n "$CURRENT_STAGE" ]; then stage "$CURRENT_STAGE" fail "line $LINENO: $BASH_COMMAND"; fi; if [ "$s" -ge 128 ] && [ "$s" -le 159 ]; then crash_capture "$s" || true; fi; exit $s' ERR
# Payload report (PAYLOAD_REPORT, payload-report.json in the working
# directory): written on every exit path from what the run left behind,
# by payload_report.py, and carried into jobReport.json by the epicprod
# dispatcher. Event counts and the note are filled in as the run goes;
# the report never changes the exit code.
REPORT_NOTE=""
FULL_EVENTS=""
RECO_EVENTS=""
FULL_EVENTS_ARGS=()
RECO_EVENTS_ARGS=()
# Sending the report out of the job (swf-epicprod docs/JOB_REPORTING.md).
# PanDA keeps job metadata for finished jobs only, so a job that dies
# reaches the production system this way or not at all. The caps are here
# rather than in configuration: a defect that sends in a loop is the only
# unbounded cost this channel has, and the fleet cannot raise these.
REPORT_SEND_MAX=12
REPORT_SENT=0
REPORT_SEND_OFF=0
# What the job calls itself when it reports. The PanDA job id when the
# server substituted one, and otherwise something unique to this run:
# object storage has no versioning here, so a shared name means one job
# silently overwrites another's reports.
REPORT_ID=${PANDAID:-}
case "${REPORT_ID}" in
  ''|*[!0-9]*) REPORT_ID="unidentified-$(hostname -s 2>/dev/null || echo host)-$$-$(date -u +%s)" ;;
esac
report_send() {
  [ -n "${REPORT_OUT_BUCKET:-}" ] || return 0
  [ "${REPORT_SEND_OFF}" -eq 0 ] || return 0
  [ "${REPORT_SENT}" -lt "${REPORT_SEND_MAX}" ] || return 0
  local here=${SCRIPT_DIR:-$(dirname "$0")}
  # A wave of jobs crosses a stage boundary together; a few seconds of
  # jitter is what keeps their writes from arriving as one pulse.
  sleep "0.$((RANDOM % 900 + 100))" 2>/dev/null || true
  if python "${here}/report_out.py" \
       --file "${PAYLOAD_REPORT:-payload-report.json}" \
       --key "reports/${REPORT_ID}/${REPORT_SENT}.json"; then
    REPORT_SENT=$((REPORT_SENT + 1))
  else
    # One failure is enough: a channel that is unreachable now stays
    # unreachable for this job, and retrying costs wall time for nothing.
    REPORT_SEND_OFF=1
    echo "report sending disabled for this job after a failed write"
  fi
}
payload_report() {
  local rc=$1; shift
  local here=${SCRIPT_DIR:-$(dirname "$0")}
  # An exit code only when the run has ended; the refresh at each stage
  # reports where the payload has got to, with no exit yet. IFS carries no
  # space here, so optional arguments travel in an array.
  local ended=()
  [ -n "${rc}" ] && ended=(--exit "${rc}")
  python "${here}/payload_report.py" --out "${PAYLOAD_REPORT:-payload-report.json}" \
    ${ended[@]+"${ended[@]}"} "$@" \
    --job-report "${PAYLOAD_JOB_REPORT:-jobReport.json}" \
    --stages "${PAYLOAD_STAGES_LOG:-payload-stages.log}" --version "${here}/VERSION" \
    --stash "${STASH_OUT:-}" \
    --requested "${EVENTS_PER_TASK:-}" --prmon-dir "${LOG_TEMP:-}" --taskname "${TASKNAME:-}" \
    --full "${FULL_TEMP:+${FULL_TEMP}/${TASKNAME:-}.edm4hep.root}" \
    --reco "${RECO_TEMP:+${RECO_TEMP}/${TASKNAME:-}.eicrecon.edm4eic.root}" \
    --full-events "${FULL_EVENTS}" --reco-events "${RECO_EVENTS}" --note "${REPORT_NOTE}" \
    || echo "payload report not written (payload_report.py exit $?)"
  report_send || true
}
trap 'payload_report $?' EXIT
IFS=$'\n\t'

# Load bearer token or fall back to x509 proxy for xrootd authentication
setup_xrd_auth() {
  echo "BEARER_TOKEN file location: ${_CONDOR_CREDS:-.}/eic.use"
  if [ -f "${_CONDOR_CREDS:-.}/eic.use" ]; then
    export BEARER_TOKEN=$(cat ${_CONDOR_CREDS:-.}/eic.use)
    echo "BEARER_TOKEN loaded successfully"
  else
    echo "WARNING: BEARER_TOKEN file not found at ${_CONDOR_CREDS:-.}/eic.use"
    if [ -f "x509_user_proxy" ]; then
      echo "Found x509_user_proxy, setting X509_USER_PROXY"
      export X509_USER_PROXY="x509_user_proxy"
    fi
  fi
}

# Load job environment (mask secrets: any line naming a token, a secret,
# a password or an access key stays out of the job's stdout)
if ls environment*.sh ; then
  grep -v -E 'BEARER|SECRET|TOKEN|PASSWORD|ACCESS_KEY' environment*.sh
  source environment*.sh
fi

# Check arguments
if [ $# -lt 1 ] ; then
  echo "Usage: "
  echo "  $0 <input> [n_chunk=10000] [i_chunk=]"
  echo
  echo "A typical npsim run requires from 0.5 to 5 core-seconds per event,"
  echo "and uses under 3 GB of memory. The output ROOT file for"
  echo "10k events take up about 2 GB in disk space."
  exit
fi

# Startup
echo "date sys: $(date)"
echo "date web: $(date -d "$(curl --insecure --head --silent --max-redirs 0 google.com 2>&1 | grep Date: | cut -d' ' -f2-7)")"
echo "hostname: $(hostname -f)"
echo "uname:    $(uname -a)"
echo "whoami:   $(whoami)"
echo "pwd:      $(pwd)"
echo "site:     ${GLIDEIN_Site:-}"
echo "resource: ${GLIDEIN_ResourceName:-}"
echo "http_proxy: ${http_proxy:-}"
df -h --exclude-type=fuse --exclude-type=tmpfs
ls -al
test -f .job.ad && cat .job.ad
test -f .machine.ad && cat .machine.ad

# Load container environment (include ${DETECTOR_VERSION})
export DETECTOR_CONFIG_REQUESTED=${DETECTOR_CONFIG:-}
export DETECTOR_VERSION_REQUESTED=${DETECTOR_VERSION:-main}
source /opt/detector/epic-${DETECTOR_VERSION_REQUESTED}/bin/thisepic.sh
export DETECTOR_VERSION=${DETECTOR_VERSION_REQUESTED}
export DETECTOR_CONFIG=${DETECTOR_CONFIG_REQUESTED:-${DETECTOR_CONFIG:-$DETECTOR}}
export SCRIPT_DIR=$(realpath $(dirname $0))
export RUCIO_CONFIG=$SCRIPT_DIR/rucio.cfg
export RUCIO_ACCOUNT=eicprod

# Print out the location of the rucio config file
echo $RUCIO_CONFIG

# Argument parsing
# - input file basename
BASENAME=${1}
# - input file extension to determine type of simulation
EXTENSION=${2}
# - number of events
EVENTS_PER_TASK=${3:-10000}
# - current chunk (zero-based)
if [ ${#} -lt 4 ] ; then
  TASK=""
  SEED=1
  SKIP_N_EVENTS=0
else
  # 10-base input task number to 4-zero-padded task number
  TASK=".${4}"
  SEED=$((10#${4}+1))
  # assumes zero-based task number, can be zero-padded 
  SKIP_N_EVENTS=$((10#${4}*EVENTS_PER_TASK))
fi

# Output location
BASEDIR=${DATADIR:-${PWD}}

# XRD Write locations (allow for empty URL override)
XRDWURL=${XRDWURL-"xroots://dtn2201.jlab.org/"}
XRDWBASE=${XRDWBASE:-"/eic/eic2/EPIC"}

# XRD Read locations (allow for empty URL override)
XRDRURL=${XRDRURL-"root://dtn-eic.jlab.org/"}
XRDRBASE=${XRDRBASE:-"/volatile/eic/EPIC"}

# Local temp dir
echo "SLURM_TMPDIR=${SLURM_TMPDIR:-}"
echo "SLURM_JOB_ID=${SLURM_JOB_ID:-}"
echo "SLURM_ARRAY_JOB_ID=${SLURM_ARRAY_JOB_ID:-}"
echo "SLURM_ARRAY_TASK_ID=${SLURM_ARRAY_TASK_ID:-}"
echo "_CONDOR_SCRATCH_DIR=${_CONDOR_SCRATCH_DIR:-}"
echo "OSG_WN_TMP=${OSG_WN_TMP:-}"
if [ -n "${_CONDOR_SCRATCH_DIR:-}" ] ; then
  TMPDIR=${_CONDOR_SCRATCH_DIR}
elif [ -n "${SLURM_TMPDIR:-}" ] ; then
  TMPDIR=${SLURM_TMPDIR}
else
  if [ -d "/scratch/slurm/${SLURM_JOB_ID:-}" ] ; then
    TMPDIR="/scratch/slurm/${SLURM_JOB_ID:-}"
  else
    TMPDIR=${TMPDIR:-/tmp}/${$}
  fi
fi
echo "TMPDIR=${TMPDIR}"
mkdir -p ${TMPDIR}
ls -al ${TMPDIR}

# Input file parsing
INPUT_FILE=${BASENAME}.${EXTENSION}
TASKNAME=${TAG_SUFFIX:+${TAG_SUFFIX}_}$(basename ${BASENAME})${TASK}
INPUT_DIR=$(dirname $(realpath --canonicalize-missing --relative-to=${BASEDIR} ${INPUT_FILE}))
# - file.hepmc              -> TAG="", and avoid double // in S3 location
# - EVGEN/file.hepmc        -> TAG="", and avoid double // in S3 location
# - EVGEN/DIS/file.hepmc    -> TAG="DIS"
# - EVGEN/DIS/NC/file.hepmc -> TAG="DIS/NC"
# - ../file.hepmc           -> error
if [ ! "${INPUT_DIR/\.\.\//}" = "${INPUT_DIR}" ] ; then
  echo "Error: Input file must be below current directory."
  exit
fi
INPUT_PREFIX=${INPUT_DIR/\/*/}
TAG=${INPUT_DIR/${INPUT_PREFIX}\//}
INPUT_DIR=${BASEDIR}/EVGEN/${TAG}
mkdir -p ${INPUT_DIR}
# The sample's own path below EVGEN/, before the detector segments join
# it: where a generated sample registers (docs/EPICPROD_INTERNAL_EVGEN.md).
SAMPLE_TAG=${TAG}
TAG=${DETECTOR_VERSION:-main}/${DETECTOR_CONFIG}/${TAG_PREFIX:+${TAG_PREFIX}/}${TAG}

# The log directory holds every stage's prmon output, so it exists before
# the first stage runs.
LOG_DIR=LOG/${TAG}
LOG_TEMP=${TMPDIR}/${LOG_DIR}
mkdir -p ${LOG_TEMP}

# Landing check, before any work: can this worker reach the catalog of
# record and the input door at all. A worker that cannot reach the Rucio
# server does the whole simulation and dies at registration hours later
# with nothing delivered (trial 39305, job 2721306: 3,015 s to a TLS
# handshake timeout after the physics was done). A definite negative,
# twice, in the first seconds costs seconds instead: the payload
# declines the landing with its own exit code, the reason in the stage
# log and the report, and PanDA sends the job elsewhere. Doubt proceeds
# (site-canary DESIGN.md, the carrier that declines its landing;
# swf-epicprod docs/EPICPROD_PAYLOAD.md, exit code 80).
stage landing start
if LANDING_OUT=$(python $SCRIPT_DIR/landing_check.py 2>&1); then
  LANDING_RC=0
else
  LANDING_RC=$?
fi
echo "${LANDING_OUT}"
if [ "${LANDING_RC}" -eq 4 ]; then
  LANDING_REASON=$(echo "${LANDING_OUT}" | grep FAILED | head -1)
  stage landing decline "${LANDING_REASON}"
  REPORT_NOTE="landing declined: ${LANDING_REASON}"
  echo "ERROR: landing declined; no work started. ${LANDING_REASON}"
  exit 80
elif [ "${LANDING_RC}" -ne 0 ]; then
  stage landing ok "check did not run (exit ${LANDING_RC}); proceeding"
else
  stage landing ok
fi

# The beam-energy geometry: the detector compact file for the requested
# beams, or for the stand-in beams DETECTOR_BEAMS names when no geometry
# exists for them (a declared wrong knob of a trial, set on the task's
# configuration; docs/EPICPROD_INTERNAL_EVGEN.md § Trials). The image
# carries compact files for the nominal ep energies and a few eA ones,
# and npsim exits 1 for any other pair after the generation has run (trial
# 39362, job 2721364: 5x130 has none). Checked here instead, in the first
# seconds, with its own exit code (83).
DETECTOR_BEAMS=${DETECTOR_BEAMS:-${EBEAM:+${PBEAM:+${EBEAM}x${PBEAM}}}}
export DETECTOR_COMPACT=${DETECTOR_PATH}/${DETECTOR_CONFIG}${DETECTOR_BEAMS:+_${DETECTOR_BEAMS}}.xml
stage geometry start
if [ -f "${DETECTOR_COMPACT}" ]; then
  if [ "${DETECTOR_BEAMS}" != "${EBEAM:-}x${PBEAM:-}" ]; then
    stage geometry ok "$(basename ${DETECTOR_COMPACT}), a stand-in for beams ${EBEAM:-?}x${PBEAM:-?}"
  else
    stage geometry ok "$(basename ${DETECTOR_COMPACT})"
  fi
else
  stage geometry fail "${DETECTOR_COMPACT} does not exist"
  REPORT_NOTE="no detector geometry for beams ${DETECTOR_BEAMS}: $(basename ${DETECTOR_COMPACT}) is not in the image"
  echo "ERROR: no detector geometry ${DETECTOR_COMPACT}; no work started."
  exit 83
fi

# Internal EVGEN (docs/EPICPROD_INTERNAL_EVGEN.md): the job generates the
# sample it simulates, at the path an externally supplied one would have
# had, so the input stage and everything after it run unchanged. The
# steering is composed from the generation environment the submission
# wrote; the stage records the events generated and the seed. A
# generation that fails is its own exit code (82): nothing downstream
# has run and the report says which step refused.
EVGEN_SUMMARY=""
if [ "${EVGEN_INTERNAL:-false}" == "true" ]; then
  stage evgen start
  EVGEN_LOCAL=${INPUT_DIR}/$(basename ${INPUT_FILE})
  EVGEN_WORK=${TMPDIR}/evgen
  EVGEN_SUMMARY=${EVGEN_WORK}/evgen-summary.json
  mkdir -p ${EVGEN_WORK}
  if monitor evgen python $SCRIPT_DIR/evgen_generate.py \
       --out "${EVGEN_LOCAL}" --events "${EVENTS_PER_TASK}" \
       --workdir "${EVGEN_WORK}" --log "${LOG_TEMP}/${TASKNAME}.evgen.log" \
       --summary "${EVGEN_SUMMARY}"; then
    EVGEN_EVENTS=$(jq -r '.events // .events_requested' "${EVGEN_SUMMARY}" 2>/dev/null || true)
    EVGEN_SEED=$(jq -r '.seed' "${EVGEN_SUMMARY}" 2>/dev/null || true)
    stage evgen ok "${EVGEN_EVENTS:-?} events, seed ${EVGEN_SEED:-?}, $(basename ${EVGEN_LOCAL})"
  else
    EVGEN_RC=$?
    stage evgen fail "evgen_generate.py exit ${EVGEN_RC}"
    REPORT_NOTE="event generation failed (evgen_generate.py exit ${EVGEN_RC})"
    echo "ERROR: event generation failed; nothing downstream has run."
    exit 82
  fi
fi

stage input start
if [ "${EVGEN_INTERNAL:-false}" == "true" ]; then
  # The sample generated above, in the place a streamed one is read from.
  INPUT_FILE=${EVGEN_LOCAL}
elif [[ "$EXTENSION" == "hepmc3.tree.root" ]]; then
  # Define location on xrootd from where to stream input file from
  INPUT_FILE=${XRDRURL}/${XRDRBASE}/${INPUT_FILE}
else
  # Copy input file from xrootd
  monitor input xrdcp -f ${XRDRURL}/${XRDRBASE}/${INPUT_FILE} ${INPUT_DIR}
fi
stage input ok

# Output file names
#
FULL_DIR=FULL/${TAG}
FULL_TEMP=${TMPDIR}/${FULL_DIR}
mkdir -p ${FULL_TEMP} 
#
RECO_DIR=RECO/${TAG}
RECO_TEMP=${TMPDIR}/${RECO_DIR}
mkdir -p ${RECO_TEMP}
#
# A generated sample registers in the EVGEN layout, without the detector
# segments: the sample is what it is whatever simulates it.
EVGEN_DIR=EVGEN/${SAMPLE_TAG}

# Canary payload run (site-canary IMPLEMENTATION.md, Payload canaries):
# the FULL and RECO files go to one flat dataset under epic:/TEST/, named
# by the run, with a lifetime on everything registered; no log upload to
# JLab, the PanDA log dataset carries the logs. The production layout is
# not reproduced, and no production record reads /TEST.
LIFETIME_ARGS=()
if [[ -n "${CANARY_OUTPUT_DATASET:-}" ]]; then
  FULL_DIR=${CANARY_OUTPUT_DATASET}
  FULL_TEMP=${TMPDIR}/${FULL_DIR}
  RECO_DIR=${CANARY_OUTPUT_DATASET}
  RECO_TEMP=${TMPDIR}/${RECO_DIR}
  EVGEN_DIR=${CANARY_OUTPUT_DATASET}
  mkdir -p ${FULL_TEMP} ${RECO_TEMP}
  COPYLOG=false
  # An array, not an unquoted expansion: IFS above holds no space, so
  # "--lifetime N" would reach the registration script as one word.
  if [[ -n "${CANARY_LIFETIME_S:-}" ]]; then
    LIFETIME_ARGS=(--lifetime "${CANARY_LIFETIME_S}")
  fi
  echo "canary payload run: outputs to epic:/${CANARY_OUTPUT_DATASET}, lifetime ${CANARY_LIFETIME_S:-unset} s, no log upload"
fi

# Trial run (docs/PCS.md, Trials): a small, real run of a composed
# configuration, offered to the requesting physics group. Unlike the
# canary it keeps the production layout — the same FULL, RECO and LOG
# substructure — rooted under epic:/TEST/trial, so the group reads the
# output in the shape real data has while nothing can mistake it for
# production. Logs are uploaded as production uploads them, under the
# same root. Everything registered carries a lifetime: what survives a
# trial is the acceptance and the record, not the data.
if [[ -n "${TRIAL_OUTPUT_ROOT:-}" ]]; then
  FULL_DIR=${TRIAL_OUTPUT_ROOT}/FULL/${TAG}
  FULL_TEMP=${TMPDIR}/${FULL_DIR}
  RECO_DIR=${TRIAL_OUTPUT_ROOT}/RECO/${TAG}
  RECO_TEMP=${TMPDIR}/${RECO_DIR}
  LOG_DIR=${TRIAL_OUTPUT_ROOT}/LOG/${TAG}
  EARLY_LOG_TEMP=${LOG_TEMP}
  LOG_TEMP=${TMPDIR}/${LOG_DIR}
  EVGEN_DIR=${TRIAL_OUTPUT_ROOT}/EVGEN/${SAMPLE_TAG}
  mkdir -p ${FULL_TEMP} ${RECO_TEMP} ${LOG_TEMP}
  # The stages that ran before this point (generation) logged under the
  # production log directory; their logs belong with the trial's.
  if [ -d "${EARLY_LOG_TEMP}" ] && [ "${EARLY_LOG_TEMP}" != "${LOG_TEMP}" ]; then
    find "${EARLY_LOG_TEMP}" -maxdepth 1 -type f -exec mv -t "${LOG_TEMP}/" {} + 2>/dev/null || true
  fi
  if [[ -n "${TRIAL_LIFETIME_S:-}" ]]; then
    LIFETIME_ARGS=(--lifetime "${TRIAL_LIFETIME_S}")
  fi
  echo "trial payload run: outputs under epic:/${TRIAL_OUTPUT_ROOT} in the production layout, lifetime ${TRIAL_LIFETIME_S:-unset} s"
fi

# Before any work, ask the catalog of record about this job's output. A
# retry of a job whose earlier attempt already delivered the RECO file has
# nothing to do and exits success here, in seconds, instead of repeating
# the simulation and failing at registration on the existing DID. An
# output name held by a failed earlier attempt (registered, no available
# replica) cannot be regenerated under this name: it stops here with its
# own exit code, and the residual rerun as a new try is the route
# (epicprod payload: swf-epicprod docs/EPICPROD_PAYLOAD.md).
# The check is authoritative, not merely existential: it carries the event
# count this job is to produce, so a registered replica holding a different
# count is not this job's delivered output and the work goes ahead. Both
# produced outputs are asked about; the job has nothing to do only when
# everything it would deliver is already delivered
# (docs/RUCIO_RESILIENCE.md, Measure 3).
RECO_DID=/${RECO_DIR}/${TASKNAME}.eicrecon.edm4eic.root
FULL_DID=/${FULL_DIR}/${TASKNAME}.edm4hep.root
# Where a diverted registration leaves the name it actually used.
export DIVERTED_OUT=${TMPDIR}/${TASKNAME}.diverted

# The failover stash (docs/RUCIO_FAILOVER_STASH.md). When the JLab upload
# path itself fails — the door unreachable, the catalog refusing to
# authenticate — the output has nowhere to go and finished physics is lost
# for want of a destination. The stash is BNL-XRD, the BNL science-data
# RSE of the catalog of record, written by xrdcp with the production
# credential the job already carries, at the path the RSE's deterministic
# naming gives the file's logical name. The file is therefore already
# home; the catalog work, the replica row, is left to the registrar, which
# adds it where the file lies when JLab answers. Nothing is moved.
STASH_DOOR=${STASH_DOOR:-"root://epicxrd1.sdcc.bnl.gov:1094"}
STASH_PREFIX=${STASH_PREFIX:-"/eic/EPIC"}
STASH_OUT=${TMPDIR}/${TASKNAME}.stash

stash_output() {
  # $1 local file, $2 the DID it owes JLab, $3 why we are stashing
  local file=$1 destination=$2 reason=$3
  local target="${STASH_PREFIX}/${destination#/}"
  stage stash start "$(basename "${file}")"
  if [ ! -f "${file}" ]; then
    stage stash fail "the output is not on disk to stash: ${file}"
    return 1
  fi
  if timeout "${STASH_TIMEOUT:-600}" xrdcp -f "${file}" "${STASH_DOOR}/${target}"; then
    # The report is what the registrar reads: the logical name, the path
    # it sits at, what it owes (the same name, registered), and why.
    printf '%s\t%s\t%s\t%s\n' "${destination}" "${target}" "${destination}" "${reason}" \
      >> "${STASH_OUT}"
    stage stash ok "${destination} at BNL-XRD, registration owed"
    echo "stashed ${file} at ${STASH_DOOR}/${target}; its registration is owed"
    return 0
  fi
  stage stash fail "xrdcp to ${STASH_DOOR}/${target} failed"
  return 1
}
OUTPUT_STATE=$(python $SCRIPT_DIR/check_output.py epic ${RECO_DID} "${EVENTS_PER_TASK:-}" || echo UNKNOWN)
FULL_STATE=SKIPPED
if [ "${COPYFULL:-false}" == "true" ] && [ "${USERUCIO:-false}" == "true" ]; then
  FULL_STATE=$(python $SCRIPT_DIR/check_output.py epic ${FULL_DID} "${EVENTS_PER_TASK:-}" || echo UNKNOWN)
  echo "FULL output ${FULL_DID}: ${FULL_STATE}"
fi
case "${OUTPUT_STATE}" in
  AVAILABLE)
    if [ "${FULL_STATE}" == "SKIPPED" ] || [ "${FULL_STATE}" == "AVAILABLE" ]; then
      echo "Output ${RECO_DID} is registered with an available replica: delivered by an earlier attempt of this job; nothing to do."
      REPORT_NOTE="output delivered by an earlier attempt of this job"
      exit 0
    fi
    echo "RECO is delivered but FULL is ${FULL_STATE}: the work unit is incomplete, proceeding."
    ;;
  HELD)
    echo "ERROR: output name ${RECO_DID} is held by a failed earlier attempt (registered, no available replica) and cannot be regenerated under this name; rerun the residual as a new try."
    REPORT_NOTE="output name held by a failed earlier attempt"
    exit 79 ;;
  MISMATCH)
    # Registered, available, and not what this job is to produce. The work
    # goes ahead and registration diverts to a derived name rather than
    # discarding validated data over a naming rule.
    echo "Output ${RECO_DID} is registered with different content (event count differs); proceeding, and registration will divert to a derived name."
    REPORT_NOTE="the output name holds different content; registering under a derived name"
    ;;
  *)
    echo "Output ${RECO_DID} not registered (${OUTPUT_STATE}); proceeding." ;;
esac

# Integration window (ns) used with bg freq (kHz) to compute per-event skip
INTEGRATION_WINDOW=${INTEGRATION_WINDOW:-2000}

# Mix background events if the input file is a hepmc file
if [[ "$EXTENSION" == "hepmc3.tree.root" ]]; then
  BG_ARGS=()

  SIGNAL_STATUS_VALUE=${SIGNAL_STATUS:-0}
  STABLE_STATUSES="$((${SIGNAL_STATUS_VALUE}+1))"
  DECAY_STATUSES="$((${SIGNAL_STATUS_VALUE}+2))"

  if [[ -n "${BG_FILES:-}" ]]; then
    while read -r bg_file; do
      file=$(echo "$bg_file" | jq -r '.file')
      freq=$(echo "$bg_file" | jq -r '.freq')

      # bg events per signal event = freq[kHz]*1e3 * INTEGRATION_WINDOW[ns]*1e-9 = freq*window*1e-6
      skip=$(awk "BEGIN {print (${freq})*(${INTEGRATION_WINDOW})*1e-6}")

      # Mix BASENAME hash into seed so different signal files decorrelate, not just task index.
      # Shift hash into high bits so it can't cancel with task-index low bits (2^20 task headroom).
      # sha256 truncated to 32 bits — better distribution than cksum/CRC for similar filenames.
      BASENAME_HASH=$(( 16#$(printf '%s' "${BASENAME}" | sha256sum | cut -c1-8) ))
      MIXED_SEED=$(( (BASENAME_HASH << 20) + SEED ))

      # Rate-scaled advance + seed-driven random offset. Rate term preserves bg-per-signal proportionality;
      # random term decorrelates parallel tasks. Merger wraps bg file, so large offsets are safe.
      skip=$(awk "BEGIN {srand(${MIXED_SEED}); print int((${SKIP_N_EVENTS}*${skip})+1) + int(rand()*2147483647)}")
      status=$(echo "$bg_file" | jq -r '.status')
      BG_ARGS+=(--bgFile "$file" "$freq" "$skip" "$status")
      STABLE_STATUSES="${STABLE_STATUSES} $((status+1))"
      DECAY_STATUSES="${DECAY_STATUSES} $((status+2))"
    done < <(jq -c '.[]' ${BG_FILES})
    # Run the background merger with proper logging
    {
      date
      eic-info
      prmon \
        --filename ${LOG_TEMP}/${TASKNAME}.hepmcmerger.prmon.txt \
        --json-summary ${LOG_TEMP}/${TASKNAME}.hepmcmerger.prmon.json \
        -- \
      SignalBackgroundMerger \
        --rngSeed ${SEED:-1} \
        --nSlices ${EVENTS_PER_TASK} \
        --signalSkip ${SKIP_N_EVENTS} \
        --signalFile ${INPUT_FILE} \
        --signalFreq ${SIGNAL_FREQ:-0} \
        --signalStatus ${SIGNAL_STATUS:-0} \
        --intWindow ${INTEGRATION_WINDOW} \
        "${BG_ARGS[@]}" \
        --outputFile ${FULL_TEMP}/${TASKNAME}.hepmc3.tree.root

    } 2>&1 | tee ${LOG_TEMP}/${TASKNAME}.hepmcmerger.log | tail -n1000

    # Use background merged file as input for next stage
    INPUT_FILE=${FULL_TEMP}/${TASKNAME}.hepmc3.tree.root
    # Don't skip events on the background merged file
    SKIP_N_EVENTS=0
  else
    echo "No background mixing will be performed since no sources are provided"
  fi
else
  echo "No background mixing is performed for singles"
fi

# Run simulation
stage simulation start "${EVENTS_PER_TASK:-?} events"
{
  date
  eic-info
  # Common flags shared by both types of simulation
  common_flags=(
    --random.seed ${SEED:-1}
    --random.enableEventSeed
    --printLevel WARNING
    --filter.tracker 'edep0'
    --numberOfEvents ${EVENTS_PER_TASK}
    --compactFile ${DETECTOR_COMPACT}
    --outputFile ${FULL_TEMP}/${TASKNAME}.edm4hep.root
  )
  # Uncommon flags based on EXTENSION
  if [[ "$EXTENSION" == "hepmc3.tree.root" ]]; then
    uncommon_flags=(
      --runType batch
      --skipNEvents ${SKIP_N_EVENTS}
      --hepmc3.useHepMC3 ${USEHEPMC3:-true}
      --physics.alternativeStableStatuses "${STABLE_STATUSES}"
      --physics.alternativeDecayStatuses "${DECAY_STATUSES}"
      --inputFiles ${INPUT_FILE}
    )
  else
    uncommon_flags=(
      --runType run
      --enableGun
      --steeringFile ${INPUT_FILE}
    )
  fi
  # Run npsim with both common and uncommon flags
  prmon \
    --filename ${LOG_TEMP}/${TASKNAME}.npsim.prmon.txt \
    --json-summary ${LOG_TEMP}/${TASKNAME}.npsim.prmon.json \
    --log-filename ${LOG_TEMP}/${TASKNAME}.npsim.prmon.log \
    -- \
  npsim "${common_flags[@]}" "${uncommon_flags[@]}"
  ls -al ${FULL_TEMP}/${TASKNAME}.edm4hep.root
} 2>&1 | tee ${LOG_TEMP}/${TASKNAME}.npsim.log | tail -n1000
stage simulation ok
# The simulated event count, from the file: reported, and registered on
# the FULL DID when the file is (RUCIO_REGISTRATION_CONTRACT.md).
FULL_EVENTS=$(python $SCRIPT_DIR/count_events.py "${FULL_TEMP}/${TASKNAME}.edm4hep.root") || FULL_EVENTS=""
if [ -n "${FULL_EVENTS}" ]; then
  FULL_EVENTS_ARGS=(--events "${FULL_EVENTS}")
  stage events ok "FULL ${FULL_EVENTS}"
else
  stage events fail FULL
fi

# Run eicrecon reconstruction
stage reconstruction start
{
  date
  eic-info
  prmon \
    --filename ${LOG_TEMP}/${TASKNAME}.eicrecon.prmon.txt \
    --json-summary ${LOG_TEMP}/${TASKNAME}.eicrecon.prmon.json \
    --log-filename ${LOG_TEMP}/${TASKNAME}.eicrecon.prmon.log \
    -- \
  eicrecon \
    -Pdd4hep:xml_files="${DETECTOR_COMPACT}" \
    -Ppodio:output_file="${RECO_TEMP}/${TASKNAME}.eicrecon.edm4eic.root" \
    -Pjana:warmup_timeout=0 -Pjana:timeout=0 \
    -Pplugins=janadot \
    "${FULL_TEMP}/${TASKNAME}.edm4hep.root"
  if [ -f jana.dot ] ; then mv jana.dot ${LOG_TEMP}/${TASKNAME}.eicrecon.dot ; fi
  ls -al ${RECO_TEMP}/${TASKNAME}.eicrecon.edm4eic.root
} 2>&1 | tee ${LOG_TEMP}/${TASKNAME}.eicrecon.log | tail -n1000
stage reconstruction ok
# The reconstructed event count, the count a job delivers. When the
# output is copied, the validation below takes it from the ROOT open it
# already performs and writes it here, so the job pays no second open;
# when it is not, the count is taken here so the report carries it either
# way.
RECO_EVENTS_FILE=${TMPDIR}/reco-events.txt
if [ "${COPYRECO:-false}" != "true" ]; then
  RECO_EVENTS=$(python $SCRIPT_DIR/count_events.py "${RECO_TEMP}/${TASKNAME}.eicrecon.edm4eic.root") || RECO_EVENTS=""
  if [ -n "${RECO_EVENTS}" ]; then
    stage events ok "RECO ${RECO_EVENTS}"
  else
    stage events fail RECO
  fi
fi

# List log files
ls -al ${LOG_TEMP}/${TASKNAME}.*

# Build metadata JSON string for Rucio registration
stage metadata start
# Extract software release from eic-info: strip trailing (-default)?-<40hexchars> in one pass
JUG_XL_TAG=$(eic-info 2>/dev/null | grep -oP '(?<=jug_dev: )([\d.]+-(?=stable)|.*?\K)(stable|unstable|nightly|default)')
# Extract metadata from FULL file via podio (all fields except software_release)
PODIO_ARGS=("${FULL_TEMP}/${TASKNAME}.edm4hep.root")
if [[ "$EXTENSION" != "hepmc3.tree.root" ]]; then
  PODIO_ARGS+=(--gun)
fi
if [[ "${BASENAME}" == *"BACKGROUNDS"* ]]; then
  PODIO_ARGS+=(--no-beam)
fi
# The script writes its JSON to a file rather than to stdout, because
# stdout here belongs to the monitor as well and anything the monitor
# says would be read as metadata.
PODIO_JSON_FILE=${TMPDIR}/podio-metadata.json
monitor metadata python $SCRIPT_DIR/parse_podio_metadata.py "${PODIO_ARGS[@]}" --out "${PODIO_JSON_FILE}"
PODIO_JSON=$(cat "${PODIO_JSON_FILE}" 2>/dev/null || true)
if ! echo "${PODIO_JSON}" | jq -e . >/dev/null 2>&1; then
  echo "ERROR: podio metadata was not produced as JSON; nothing can be registered without it."
  stage metadata fail
  exit 66
fi

# Only software_release remains outside podio scope
METADATA_JSON_BASE=$(jq -n --arg software_release "${JUG_XL_TAG}" '{software_release: $software_release}')

# Merge podio-extracted fields (geometry_config, data_level, beam/gun params)
METADATA_JSON_FULL=$(jq -n --argjson base "${METADATA_JSON_BASE}" --argjson podio "${PODIO_JSON}" '$base * $podio')
METADATA_JSON_RECO=$(jq -n --argjson base "${METADATA_JSON_FULL}" '$base | .data_level = "reconstruction"')
stage metadata ok

# Data egress to directory

# The Rucio log upload, as a function: the Logs stage calls it on a
# finished run, and the crash trap calls it on a crash-class exit so a
# crashed job's stage logs reach the store as well.
upload_logs() {
    # Every path through this block leaves a mark in the stage log. The
    # block used to record only its failures, so a successful upload, a
    # block never entered, and a configuration that skips logs all read
    # as the same absence: a trial could not say whether it had
    # exercised the log path it exists to prove, and a job killed during
    # an upload reported no log stage and none of the time it spent
    # there. Job 2721306 spent twenty minutes here, ten on each attempt.
    stage logs start
    TIME_TAG=$(date --iso-8601=second)
    TARFILE="${LOG_TEMP}/${TASKNAME}.log.tar.gz"

    # Initialize an empty array to hold existing files
    FILES_TO_TAR=()

    # List of expected files
    for FILE in \
      "${LOG_TEMP}/${TASKNAME}.evgen.log" \
      "${LOG_TEMP}/${TASKNAME}.evgen.prmon.txt" \
      "${LOG_TEMP}/${TASKNAME}.npsim.prmon.txt" \
      "${LOG_TEMP}/${TASKNAME}.npsim.log" \
      "${LOG_TEMP}/${TASKNAME}.eicrecon.prmon.txt" \
      "${LOG_TEMP}/${TASKNAME}.eicrecon.log" \
      "${LOG_TEMP}/${TASKNAME}.eicrecon.dot" \
      "${LOG_TEMP}/${TASKNAME}.hepmcmerger.log"
    do
      if [ -f "$FILE" ]; then
        FILES_TO_TAR+=("$FILE")
      fi
    done

    # Create the tar archive only if there are files to include
    if [ ${#FILES_TO_TAR[@]} -gt 0 ]; then
      tar -czvf "$TARFILE" "${FILES_TO_TAR[@]}"
    else
      echo "No log files found to archive."
    fi
    
    # A log upload NEVER kills a job. The physics is already made and
    # validated by the time this runs; losing it because a log tarball
    # could not be written is the most expensive possible way to lose
    # nothing of value. On 2026-08-08 exactly that destroyed three
    # attempts at 9x275 q2_10to100 — every job reconstructed, then
    # refused at EIC-XRD-LOG with HTTP 403, roughly 17,000 core-hours
    # for want of a log. So: try the log store, and if it refuses, put
    # the log beside the science data instead, and if that refuses too,
    # say so and carry on to deliver the physics.
    # An unset LOG_RSE used to fall through to the literal string
    # "isLogRSE", which is not a storage element and never was: every job
    # on a configuration with no log_rse spent an upload attempt, and the
    # timeout behind it, on a certainty. A configuration that names no log
    # store goes straight to the output store instead.
    LOG_UPLOADED=0
    if [ -n "${LOG_RSE:-}" ]; then
      if monitor logs python $SCRIPT_DIR/register_to_rucio.py \
          -f "${LOG_TEMP}/${TASKNAME}.log.tar.gz" \
          -d "/${LOG_DIR}/${TASKNAME}.${TIME_TAG}.log.tar.gz" \
          -s epic -r "${LOG_RSE}" --noregister; then
        LOG_UPLOADED=1
        stage logs ok "log uploaded to ${LOG_RSE}"
      else
        echo "WARNING: log upload to ${LOG_RSE} failed; trying the output store."
      fi
    else
      echo "No log RSE configured (log_rse unset on the production config); writing the log beside the science data."
    fi
    if [ "${LOG_UPLOADED}" -eq 0 ]; then
      if monitor logs_fallback python $SCRIPT_DIR/register_to_rucio.py \
          -f "${LOG_TEMP}/${TASKNAME}.log.tar.gz" \
          -d "/${RECO_DIR}/${TASKNAME}.${TIME_TAG}.log.tar.gz" \
          -s epic -r ${OUT_RSE:-EIC-XRD} --noregister; then
        stage logs fallback "log written beside the science data at ${OUT_RSE:-EIC-XRD}"
      else
        stage logs fail "log upload failed${LOG_RSE:+ at ${LOG_RSE}} and at ${OUT_RSE:-EIC-XRD}; the job's physics is unaffected"
        echo "WARNING: no log upload succeeded. The payload continues; the report carries this."
      fi
    fi
}

if [ "${COPYLOG:-false}" == "true" ] ; then
  if [ "${USERUCIO:-false}" == "true" ] ; then
    upload_logs
  else
    echo "=== DEBUG: Attempting to copy LOG files to xrootd ==="
    setup_xrd_auth
    echo "Source: ${LOG_TEMP}/${TASKNAME}.*"
    echo "Destination: ${XRDWURL}/${XRDWBASE}/${LOG_DIR}"
    if [ -n ${XRDWURL} ] ; then
      echo "Creating directory: xrdfs ${XRDWURL} mkdir -p ${XRDWBASE}/${LOG_DIR}"
      xrdfs ${XRDWURL} mkdir -p ${XRDWBASE}/${LOG_DIR} || echo "ERROR: Cannot create log directory on xrootd server"
    fi
    echo "Running: xrdcp --debug 2 --force --recursive ${LOG_TEMP}/${TASKNAME}.* ${XRDWURL}/${XRDWBASE}/${LOG_DIR}"
    xrdcp --debug 2 --force --recursive ${LOG_TEMP}/${TASKNAME}.* ${XRDWURL}/${XRDWBASE}/${LOG_DIR} || echo "ERROR: xrdcp failed with exit code $?"
    echo "=== DEBUG: LOG copy attempt completed ==="
    stage logs xrootd "logs copied to ${XRDWURL}/${XRDWBASE}/${LOG_DIR}"
  fi
else
  stage logs skipped "copy_log is false on this configuration"
fi

if [ "${COPYFULL:-false}" == "true" ] ; then
  # Validate ROOT file before transfer
  echo "=== Validating FULL ROOT file before transfer ==="
  monitor validation_full python $SCRIPT_DIR/validate_rootfile.py "${FULL_TEMP}/${TASKNAME}.edm4hep.root"
  if [ $? -ne 0 ]; then
    echo "ERROR: FULL ROOT file validation failed. Skipping transfer."
    stage validation fail FULL
    exit 65
  fi
  echo "FULL ROOT file validation passed."
  stage validation ok FULL

  if [ "${USERUCIO:-false}" == "true" ] ; then
    stage registration start FULL
    # A registration failure must not destroy finished physics. The script
    # exits 81 when the catalog could neither register the output nor say
    # whether it is already there: the bytes are written, the record is
    # unconfirmed, and the registrar settles it later, so the stage is
    # recorded pending and the job carries on (docs/RUCIO_RESILIENCE.md,
    # Measure 2). A catalog that answers, and answers that the output is not
    # there, is a real failure and still exits 78.
    monitor registration_full python $SCRIPT_DIR/register_to_rucio.py -f "${FULL_TEMP}/${TASKNAME}.edm4hep.root" -d "/${FULL_DIR}/${TASKNAME}.edm4hep.root" -s epic -r ${OUT_RSE:-EIC-XRD} --metadata-json "${METADATA_JSON_FULL}" ${FULL_EVENTS_ARGS[@]+"${FULL_EVENTS_ARGS[@]}"} ${LIFETIME_ARGS[@]+"${LIFETIME_ARGS[@]}"}
    REG_RC=$?
    if [ ${REG_RC} -eq 0 ]; then
      stage registration ok "/${FULL_DIR}/${TASKNAME}.edm4hep.root"
    elif [ ${REG_RC} -eq 81 ]; then
      echo "WARNING: catalog unreachable for FULL; registration pending."
      stage registration pending "/${FULL_DIR}/${TASKNAME}.edm4hep.root"
    elif stash_output "${FULL_TEMP}/${TASKNAME}.edm4hep.root" \
           "${FULL_DID}" "JLab registration failed (exit ${REG_RC})"; then
      echo "FULL could not be registered at JLab and is stashed at BNL."
      REPORT_NOTE="output stashed at BNL; the registrar owes its JLab registration"
    else
      echo "ERROR: Rucio registration failed for FULL file and the stash refused it too."
      stage registration fail FULL
      exit 78
    fi
  else
    echo "=== DEBUG: Attempting to copy FULL files to xrootd ==="
    setup_xrd_auth
    echo "Source: ${FULL_TEMP}/${TASKNAME}.edm4hep.root"
    echo "Destination: ${XRDWURL}/${XRDWBASE}/${FULL_DIR}"
    if [ -n ${XRDWURL} ] ; then
      echo "Creating directory: xrdfs ${XRDWURL} mkdir -p ${XRDWBASE}/${FULL_DIR}"
      xrdfs ${XRDWURL} mkdir -p ${XRDWBASE}/${FULL_DIR} || echo "ERROR: Cannot create simulation directory on xrootd server"
    fi
    echo "Running: xrdcp --debug 2 --force --recursive ${FULL_TEMP}/${TASKNAME}.edm4hep.root ${XRDWURL}/${XRDWBASE}/${FULL_DIR}"
    xrdcp --debug 2 --force --recursive ${FULL_TEMP}/${TASKNAME}.edm4hep.root ${XRDWURL}/${XRDWBASE}/${FULL_DIR} || echo "ERROR: xrdcp failed with exit code $?"
    echo "=== DEBUG: FULL copy attempt completed ==="
  fi
fi

if [ "${COPYRECO:-false}" == "true" ] ; then
  # Validate ROOT file before transfer
  echo "=== Validating RECO ROOT file before transfer ==="
  stage validation start RECO
  monitor validation_reco python $SCRIPT_DIR/validate_rootfile.py "${RECO_TEMP}/${TASKNAME}.eicrecon.edm4eic.root" --events-file "${RECO_EVENTS_FILE}"
  if [ $? -ne 0 ]; then
    echo "ERROR: RECO ROOT file validation failed. Skipping transfer."
    stage validation fail RECO
    exit 65
  fi
  echo "RECO ROOT file validation passed."
  stage validation ok RECO

  # The count the validation took from its own open.
  RECO_EVENTS=$(cat "${RECO_EVENTS_FILE}" 2>/dev/null || true)
  if [ -n "${RECO_EVENTS}" ]; then
    RECO_EVENTS_ARGS=(--events "${RECO_EVENTS}")
    stage events ok "RECO ${RECO_EVENTS}"
  else
    echo "ERROR: the RECO event count could not be taken; registering without it."
    stage events fail RECO
  fi

  if [ "${USERUCIO:-false}" == "true" ] ; then
    stage registration start RECO
    # Pending rather than failed when the catalog cannot answer; see the FULL
    # step above and docs/RUCIO_RESILIENCE.md, Measure 2.
    monitor registration_reco python $SCRIPT_DIR/register_to_rucio.py -f "${RECO_TEMP}/${TASKNAME}.eicrecon.edm4eic.root" -d "/${RECO_DIR}/${TASKNAME}.eicrecon.edm4eic.root" -s epic -r ${OUT_RSE:-EIC-XRD} --metadata-json "${METADATA_JSON_RECO}" ${RECO_EVENTS_ARGS[@]+"${RECO_EVENTS_ARGS[@]}"} ${LIFETIME_ARGS[@]+"${LIFETIME_ARGS[@]}"}
    REG_RC=$?
    if [ ${REG_RC} -eq 0 ] && [ -s "${DIVERTED_OUT}" ]; then
      DIVERTED_DID=$(cat "${DIVERTED_OUT}")
      echo "RECO registered under a derived name: ${DIVERTED_DID}"
      stage registration diverted "${DIVERTED_DID}"
    elif [ ${REG_RC} -eq 0 ]; then
      stage registration ok "/${RECO_DIR}/${TASKNAME}.eicrecon.edm4eic.root"
    elif [ ${REG_RC} -eq 81 ]; then
      echo "WARNING: catalog unreachable for RECO; registration pending."
      stage registration pending "/${RECO_DIR}/${TASKNAME}.eicrecon.edm4eic.root"
    elif stash_output "${RECO_TEMP}/${TASKNAME}.eicrecon.edm4eic.root" \
           "${RECO_DID}" "JLab registration failed (exit ${REG_RC})"; then
      echo "RECO could not be registered at JLab and is stashed at BNL."
      REPORT_NOTE="output stashed at BNL; the registrar owes its JLab registration"
    else
      echo "ERROR: Rucio registration failed for RECO file and the stash refused it too."
      stage registration fail RECO
      exit 78
    fi
  else
    echo "=== DEBUG: Attempting to copy RECO files to xrootd ==="
    setup_xrd_auth
    echo "Source: ${RECO_TEMP}/${TASKNAME}*.edm4eic.root"
    echo "Destination: ${XRDWURL}/${XRDWBASE}/${RECO_DIR}"
    if [ -n ${XRDWURL} ] ; then
      echo "Creating directory: xrdfs ${XRDWURL} mkdir -p ${XRDWBASE}/${RECO_DIR}"
      xrdfs ${XRDWURL} mkdir -p ${XRDWBASE}/${RECO_DIR} || echo "ERROR: Cannot create reconstruction directory on xrootd server"
    fi
    echo "Running: xrdcp --debug 2 --force --recursive ${RECO_TEMP}/${TASKNAME}*.edm4eic.root ${XRDWURL}/${XRDWBASE}/${RECO_DIR}"
    xrdcp --debug 2 --force --recursive ${RECO_TEMP}/${TASKNAME}*.edm4eic.root ${XRDWURL}/${XRDWBASE}/${RECO_DIR} || echo "ERROR: xrdcp failed with exit code $?"
    echo "=== DEBUG: RECO copy attempt completed ==="
  fi
fi

# A generated sample is an output when the configuration says so
# (docs/EPICPROD_INTERNAL_EVGEN.md): registered under EVGEN/ in the
# output layout with its event count and the same lifetime treatment as
# the physics, so a PCS-produced generator sample is an ordinary dataset
# with stage evgen. It never fails the job: the physics is delivered by
# this point, and a sample that could not be registered is said in the
# stage log and the report.
if [ "${EVGEN_INTERNAL:-false}" == "true" ] && [ "${COPYEVGEN:-false}" == "true" ] && [ "${USERUCIO:-false}" == "true" ] ; then
  EVGEN_NAME=$(basename ${EVGEN_LOCAL})
  EVGEN_EVENTS_ARGS=()
  if [ -n "${EVGEN_EVENTS:-}" ] && [ "${EVGEN_EVENTS}" != "null" ]; then
    EVGEN_EVENTS_ARGS=(--events "${EVGEN_EVENTS}")
  fi
  stage registration start EVGEN
  monitor registration_evgen python $SCRIPT_DIR/register_to_rucio.py -f "${EVGEN_LOCAL}" -d "/${EVGEN_DIR}/${EVGEN_NAME}" -s epic -r ${OUT_RSE:-EIC-XRD} ${EVGEN_EVENTS_ARGS[@]+"${EVGEN_EVENTS_ARGS[@]}"} ${LIFETIME_ARGS[@]+"${LIFETIME_ARGS[@]}"}
  REG_RC=$?
  if [ ${REG_RC} -eq 0 ]; then
    stage registration ok "/${EVGEN_DIR}/${EVGEN_NAME}"
  elif [ ${REG_RC} -eq 81 ]; then
    echo "WARNING: catalog unreachable for EVGEN; registration pending."
    stage registration pending "/${EVGEN_DIR}/${EVGEN_NAME}"
  else
    echo "WARNING: the generated sample could not be registered (exit ${REG_RC}); the job's physics is unaffected."
    stage registration fail "EVGEN exit ${REG_RC}"
  fi
fi

# closeout
date
find ${TMPDIR}
du -sh ${TMPDIR}
