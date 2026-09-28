#!/bin/bash
# Background merger adapter: SignalBackgroundMerger (HEPMC_Merger), the
# production default (swf-epicprod docs/EPICPROD_PAYLOAD.md, Background
# merging). run.sh owns the stage, the monitoring and every calculation;
# this adapter only turns the normalized arguments into the merger's
# command line and replaces itself with the merger, so the merger's exit
# status, a signal exit included, is the adapter's.
#
# Normalized arguments (the same for every adapter):
#   --seed N --frames N --window NS --output FILE
#   --signal FILE FREQ_KHZ SKIP STATUS
#   --background FILE FREQ_KHZ SKIP STATUS   (repeated, in BG_FILES order)
# --binary prints the program this adapter runs and exits.
set -euo pipefail

BINARY=SignalBackgroundMerger
if [ "${1:-}" == "--binary" ]; then
  echo "${BINARY}"
  exit 0
fi

SEED="" FRAMES="" WINDOW="" OUTPUT=""
SIG_FILE="" SIG_FREQ="" SIG_SKIP="" SIG_STATUS=""
BG_ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --seed) SEED=$2; shift 2 ;;
    --frames) FRAMES=$2; shift 2 ;;
    --window) WINDOW=$2; shift 2 ;;
    --output) OUTPUT=$2; shift 2 ;;
    --signal) SIG_FILE=$2 SIG_FREQ=$3 SIG_SKIP=$4 SIG_STATUS=$5; shift 5 ;;
    --background) BG_ARGS+=(--bgFile "$2" "$3" "$4" "$5"); shift 5 ;;
    *) echo "hepmcmerger adapter: unknown argument $1" >&2; exit 2 ;;
  esac
done

# The argument order is the one run.sh used before the adapters existed
# (payload 0.19.0 through 0.23.1), so the merger sees an identical command.
exec "${BINARY}" \
  --rngSeed "${SEED}" \
  --nSlices "${FRAMES}" \
  --signalSkip "${SIG_SKIP}" \
  --signalFile "${SIG_FILE}" \
  --signalFreq "${SIG_FREQ}" \
  --signalStatus "${SIG_STATUS}" \
  --intWindow "${WINDOW}" \
  ${BG_ARGS[@]+"${BG_ARGS[@]}"} \
  --outputFile "${OUTPUT}"
