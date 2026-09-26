#!/bin/bash
# TEST ONLY: take a running job's node away, as a preemption does
# (docs/NODE_EVENT_DISPATCHER.md, preemption; docs/NPPS0_TEST_QUEUE.md).
#
#   preempt_job.sh <PanDA job id> <close index> <seconds after it>
#
# Waits for the job's Event Service harness to start close <index>, waits
# <seconds> more (so units finish after that close and are still on the
# node), then SIGKILLs the job's whole pilot pass at once: the pilot, the
# harness, every core's container and a running close. Nothing is told,
# nothing is reported; the launcher starts its next pass as it would after
# a lost node. Only descendants of the pass that holds the job are touched.
set -u
PANDAID=${1:?PanDA job id}
CLOSE=${2:?close index}
AFTER=${3:?seconds after the close}
WAIT_S=${WAIT_S:-21600}
log() { echo "[preempt $(date '+%F %T')] $*"; }

deadline=$(( $(date +%s) + WAIT_S ))
jobdir=''
while [ -z "$jobdir" ]; do
    jobdir=$(ls -d "$HOME"/pilot-work/run-*/PanDA_Pilot-"$PANDAID" 2>/dev/null | head -1)
    [ -n "$jobdir" ] && break
    [ "$(date +%s)" -gt "$deadline" ] && { log "job $PANDAID never started here"; exit 2; }
    sleep 30
done
rundir=$(dirname "$jobdir")
log "job $PANDAID in $rundir"

closedir=$(printf '%04d' "$CLOSE")
until find "$rundir" -maxdepth 4 -type d -path "*closes/$closedir" 2>/dev/null | grep -q .; do
    [ -d "$jobdir" ] || { log "job $PANDAID ended before close $CLOSE"; exit 3; }
    [ "$(date +%s)" -gt "$deadline" ] && { log "close $CLOSE never came"; exit 2; }
    sleep 10
done
log "close $CLOSE started; cutting in $AFTER s"
sleep "$AFTER"

# The pass: the pilot wrapper running in this run directory, and the
# epicprod-gpu-pilot.sh pass above it.
wrapper=$(pgrep -f "$rundir/wrapper.sh" | head -1)
[ -n "$wrapper" ] || { log "no wrapper running for $rundir"; exit 4; }
root=$wrapper
while :; do
    parent=$(ps -o ppid= -p "$root" | tr -d ' ')
    [ -n "$parent" ] && ps -o args= -p "$parent" | grep -q 'epicprod-gpu-pilot.sh' || break
    root=$parent
done
# Every descendant of the pass, gathered before any is killed.
tree() { local p=$1; echo "$p"; for c in $(pgrep -P "$p"); do tree "$c"; done; }
pids=$(tree "$root" | sort -u | tr '\n' ' ')
log "SIGKILL $(echo $pids | wc -w) processes of the pass (root $root: $(ps -o args= -p "$root" | cut -c1-80))"
kill -9 $pids 2>/dev/null
sleep 2
left=$(for p in $pids; do [ -d /proc/$p ] && echo $p; done | tr '\n' ' ')
log "cut done at $(date +%s); still present: ${left:-none}"
