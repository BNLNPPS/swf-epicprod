#!/bin/bash
# The node harness's close (docs/NODE_EVENT_DISPATCHER.md, Package): the
# RECO files the slots handed over since the last close, merged into one
# podio file, validated, and registered under the units' own dataset,
# metadata, lifetime and preservation terms (run.sh's handoff records).
# Runs inside the task's image, in the sandbox, as run.sh does.
#
#   es_close.sh <close dir> <output name> <handoff.json> [<handoff.json> ...]
#
# Writes <close dir>/close.json: the outcome, the DID, the events, the
# files merged; exits 0 when the merged file is registered (or its
# registration is pending on an unreachable catalog, exit 81 of the
# registrar, which leaves the file home at BNL), nonzero otherwise. The
# units' files are removed after a registration that stands.
set -uo pipefail
CLOSE_DIR=$1; NAME=$2; shift 2
HERE=$(realpath "$(dirname "$0")")
PAYLOAD=$(realpath "${HERE}/..")
mkdir -p "${CLOSE_DIR}"
LOG=${CLOSE_DIR}/close.log
exec > >(tee -a "${LOG}") 2>&1
echo "close: $(date -u +%FT%TZ) ${NAME}, $# unit file(s)"

if ls environment*.sh >/dev/null 2>&1; then
  source environment*.sh
fi
export DETECTOR_VERSION_REQUESTED=${DETECTOR_VERSION:-main}
source /opt/detector/epic-${DETECTOR_VERSION_REQUESTED}/bin/thisepic.sh >/dev/null 2>&1 || true
export RUCIO_CONFIG=${PAYLOAD}/rucio.cfg RUCIO_ACCOUNT=eicprod

record() {
  # record <outcome> <did> <events> <rc> <message>
  python3 - "$CLOSE_DIR" "$1" "$2" "$3" "$4" "$5" ${FILES[@]+"${FILES[@]}"} <<'PY'
import json, sys, time
d, outcome, did, events, rc, message, *files = sys.argv[1:]
json.dump({'outcome': outcome, 'did': did, 'events': int(events) if events.isdigit() else None,
           'rc': int(rc), 'message': message, 'files': files, 'ended_at': time.time()},
          open(f'{d}/close.json', 'w'))
PY
}

FILES=(); EVENTS=0; DATASET=""; RSE=""; META=""; LIFETIME=""; PRESERVE=no; PDOOR=""; PPREFIX=""; PTIMEOUT=600
for h in "$@"; do
  f=$(jq -r '.file' "$h"); [ -s "$f" ] || { echo "ERROR: unit file missing: $f"; record failed "" 0 2 "unit file missing: $f"; exit 2; }
  FILES+=("$f")
  e=$(jq -r '.events // 0' "$h"); EVENTS=$((EVENTS + e))
  [ -n "$DATASET" ] || { DATASET=$(jq -r '.dataset' "$h"); RSE=$(jq -r '.rse' "$h"); META=$(jq -c '.metadata' "$h")
                         LIFETIME=$(jq -r '.lifetime_s // empty' "$h"); PRESERVE=$(jq -r 'if .preserve then "yes" else "no" end' "$h")
                         PDOOR=$(jq -r '.preserve_door // empty' "$h"); PPREFIX=$(jq -r '.preserve_prefix // empty' "$h"); PTIMEOUT=$(jq -r '.preserve_timeout // 600' "$h"); }
done
MERGED=${CLOSE_DIR}/${NAME}
DID=$(echo "${DATASET}/${NAME}" | sed 's#//*#/#g')     # one slash: the registrar's metadata writes take the name as given

if [ ${#FILES[@]} -eq 1 ]; then
  # One unit since the last close: its file is the close's file.
  cp "${FILES[0]}" "${MERGED}"
else
  echo "merge: podio-merge-files over ${#FILES[@]} files"
  # Its progress bars go to their own file; the log keeps the last lines.
  if ! podio-merge-files --output "${MERGED}" "${FILES[@]}" > "${CLOSE_DIR}/merge.out" 2>&1; then
    tail -c 2000 "${CLOSE_DIR}/merge.out" | tr '\r' '\n' | grep -vE '^\s*$|it/s' | tail -5
    echo "ERROR: podio-merge-files failed"; record failed "$DID" "$EVENTS" 3 "podio-merge-files failed"; exit 3
  fi
  tr '\r' '\n' < "${CLOSE_DIR}/merge.out" | grep -vE '^\s*$|it/s' | tail -3
fi
[ -s "${MERGED}" ] || { echo "ERROR: merged file missing"; record failed "$DID" "$EVENTS" 3 "merged file missing"; exit 3; }

echo "validate: ${MERGED}"
EVENTS_FILE=${CLOSE_DIR}/events.txt
if ! python "${PAYLOAD}/validate_rootfile.py" "${MERGED}" --events-file "${EVENTS_FILE}"; then
  echo "ERROR: the merged file failed validation"; record failed "$DID" "$EVENTS" 4 "merged file failed validation"; exit 4
fi
COUNTED=$(cat "${EVENTS_FILE}" 2>/dev/null || true)
if [ -n "${COUNTED}" ] && [ "${COUNTED}" != "${EVENTS}" ]; then
  echo "ERROR: the merged file holds ${COUNTED} events, the units ${EVENTS}"; record failed "$DID" "$COUNTED" 5 "merged ${COUNTED} events, units ${EVENTS}"; exit 5
fi
EVENTS=${COUNTED:-${EVENTS}}

ARGS=(-f "${MERGED}" -d "${DID}" -s epic -r "${RSE}" --metadata-json "${META}" --events "${EVENTS}")
[ -n "${LIFETIME}" ] && ARGS+=(--lifetime "${LIFETIME}")
[ "${PRESERVE}" == "yes" ] && ARGS+=(--preserve-door "${PDOOR}" --preserve-prefix "${PPREFIX}" --preserve-timeout "${PTIMEOUT}")
echo "register: ${DID} (${EVENTS} events, $(stat -c %s "${MERGED}") bytes) at ${RSE}"
export REGISTRATION_STAGGER_MAX_S=0
python "${PAYLOAD}/register_to_rucio.py" "${ARGS[@]}"; RC=$?
if [ ${RC} -eq 0 ]; then
  record registered "$DID" "$EVENTS" 0 ""
  rm -f "${FILES[@]}"
  exit 0
elif [ ${RC} -eq 81 ]; then
  record pending "$DID" "$EVENTS" 81 "catalog unreachable; the file is preserved and its registration owed"
  rm -f "${FILES[@]}"
  exit 0
fi
record failed "$DID" "$EVENTS" "${RC}" "registrar exit ${RC}"
exit ${RC}
