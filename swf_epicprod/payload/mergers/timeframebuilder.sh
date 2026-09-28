#!/bin/bash
# Background merger adapter: TimeFrameBuilder (eic/TimeframeBuilder), an
# opt-in alternative to SignalBackgroundMerger selected by
# BG_MERGER=timeframebuilder (swf-epicprod docs/EPICPROD_PAYLOAD.md,
# Background merging). run.sh owns the stage, the monitoring and every
# calculation; this adapter only turns the normalized arguments into
# TimeFrameBuilder's per-source command line and replaces itself with it.
#
# Normalized arguments (the same for every adapter):
#   --seed N --frames N --window NS --output FILE
#   --signal FILE FREQ_KHZ SKIP STATUS
#   --background FILE FREQ_KHZ SKIP STATUS   (repeated, in BG_FILES order)
# --binary prints the program this adapter runs and exits.
#
# Translation: frequencies in kHz become events per ns (x 1e-6); a signal
# frequency of 0 is one signal event per frame; each background is a
# source bgN that repeats at end of file, which the seed-driven skips
# rely on. A background frequency of 0 or less is a weighted source to
# SignalBackgroundMerger; TimeFrameBuilder has no weighted mode and would
# place no events from it, so it is refused here (exit 2).
set -euo pipefail

BINARY=timeframe_builder
if [ "${1:-}" == "--binary" ]; then
  echo "${BINARY}"
  exit 0
fi

per_ns() { awk -v f="$1" 'BEGIN { printf "%.10g", f * 1e-6 }'; }
positive() { awk -v f="$1" 'BEGIN { exit !(f > 0) }'; }

SEED="" FRAMES="" WINDOW="" OUTPUT=""
SOURCE_ARGS=()
NBG=0
while [ $# -gt 0 ]; do
  case "$1" in
    --seed) SEED=$2; shift 2 ;;
    --frames) FRAMES=$2; shift 2 ;;
    --window) WINDOW=$2; shift 2 ;;
    --output) OUTPUT=$2; shift 2 ;;
    --signal)
      SOURCE_ARGS+=(--source:signal:input_files "$2")
      if positive "$3"; then
        SOURCE_ARGS+=(--source:signal:frequency "$(per_ns "$3")")
      else
        SOURCE_ARGS+=(--source:signal:static_events true
                      --source:signal:events_per_frame 1)
      fi
      SOURCE_ARGS+=(--source:signal:skip "$4"
                    --source:signal:status_offset "$5"
                    --source:signal:keep_weight true)
      shift 5 ;;
    --background)
      if ! positive "$3"; then
        echo "timeframebuilder adapter: background $2 has frequency $3 kHz;" \
             "a weighted background has no TimeFrameBuilder equivalent" >&2
        exit 2
      fi
      NBG=$((NBG + 1))
      SOURCE_ARGS+=(--source:bg${NBG}:input_files "$2"
                    --source:bg${NBG}:frequency "$(per_ns "$3")"
                    --source:bg${NBG}:skip "$4"
                    --source:bg${NBG}:status_offset "$5"
                    --source:bg${NBG}:repeat_on_eof true)
      shift 5 ;;
    *) echo "timeframebuilder adapter: unknown argument $1" >&2; exit 2 ;;
  esac
done

exec "${BINARY}" \
  --random-seed "${SEED}" \
  --nevents "${FRAMES}" \
  --duration "${WINDOW}" \
  --output "${OUTPUT}" \
  "${SOURCE_ARGS[@]}"
