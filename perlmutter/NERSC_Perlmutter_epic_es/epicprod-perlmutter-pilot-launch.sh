#!/bin/bash
# Perlmutter's site adapter to the common ePIC native pilot. The site's Slurm
# job fetches this file from main and may still srun 80 copies; only rank zero
# starts a pilot, which carries the node's ES payload. No separate ES pilot.
# docs/NERSC_PERLMUTTER.md
set -u

[[ ${SLURM_PROCID:-0} == 0 ]] || exit 0
PQ=${1:?panda queue}
ACCESS_POINT=${2:?harvester access point}
HARVESTER_DIR=/global/common/software/m3763/panda-harvester
QUEUE_URL=https://raw.githubusercontent.com/BNLNPPS/swf-epicprod/main/perlmutter/$PQ
RUNTIME_URL=https://epic-devcloud-stageout.s3.us-east-1.amazonaws.com/pilot/native-runtime-perlmutter-es-batch-18e2802.tar.gz
RUNTIME_SHA256=22b9a74b08997a78f35bf2b06fcee1936a0dd5735306f0fbc7a009377c86f63c
PILOT_SHA256=b2655eaa69bdd96b6f359fd583a7b72b32cbf92658c3cb10badd405e66919a88
ES_CHANNEL_URL=https://epic-devcloud-stageout.s3.us-east-1.amazonaws.com/pilot/es-channel-py313-debian13.tar.gz
ES_CHANNEL_SHA256=0993145fb66b52fc759fafe01bd5cef80ead0f3fb47d721feae6aa2a61a0d0ae
IMAGE=/cvmfs/singularity.opensciencegrid.org/eicweb/eic_xl:26.07.3-stable
# One four-hour allocation, including staging and final pilot handling.
# Always respect an earlier Slurm end time.
DEADLINE_AT=$(( $(date +%s) + 14400 ))
if [[ ${SLURM_JOB_END_TIME:-} =~ ^[0-9]+$ ]] && (( SLURM_JOB_END_TIME < DEADLINE_AT )); then
    DEADLINE_AT=$SLURM_JOB_END_TIME
fi

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }

SELF=$(readlink -f "$0" 2>/dev/null || echo "$0")
WORKDIR=$ACCESS_POINT/${SLURM_JOBID:-nojob}/${SLURM_PROCID:-0}
mkdir -p "$WORKDIR" && cd "$WORKDIR" || { log "cannot enter $WORKDIR"; exit 1; }

# OURS: the launch's own account of itself, published. The site copies a
# task's output to the public worker record only when a job ran, so a
# launch that dies before its pilot takes a job leaves nothing readable
# from outside. The first four tasks of each worker therefore write
# their output to a file as well and, at exit, copy it into the
# project's web directory, which portal.nersc.gov serves:
#   https://portal.nersc.gov/cfs/m3763/panda/jobs/<queue>/launch/<worker>/task<n>.out
# A copy that cannot be made is said in the task's own output and
# changes nothing else.
LAUNCH_LOG=$WORKDIR/launch.out
LAUNCH_PUBLISH_DIR=/global/cfs/cdirs/m3763/www/panda/jobs/$PQ/launch/${HARVESTER_WORKER_ID:-noworker}
publish_launch_log() {
    local rc=$?
    log "launch exiting with code $rc"
    if [[ ${SLURM_PROCID:-0} -lt 4 ]]; then
        sleep 1   # let tee write the last lines before the copy
        if mkdir -p "$LAUNCH_PUBLISH_DIR" 2>/dev/null \
            && cp "$LAUNCH_LOG" "$LAUNCH_PUBLISH_DIR/task${SLURM_PROCID:-0}.out" 2>/dev/null; then
            chmod -R a+rX "$LAUNCH_PUBLISH_DIR" 2>/dev/null
        else
            log "launch log not published to $LAUNCH_PUBLISH_DIR"
        fi
    fi
}
exec > >(tee -a "$LAUNCH_LOG") 2>&1
trap publish_launch_log EXIT

log "queue $PQ worker ${HARVESTER_WORKER_ID:-?} harvester ${HARVESTER_ID:-?} task ${SLURM_PROCID:-0} host $(hostname -s) workdir $PWD"
log "launcher $(sha256sum "$SELF" 2>/dev/null | cut -c1-12) from $SELF"
unset TMPDIR
command -v python3 >/dev/null || log "no python3 on the host: the pool sample and the node guard read are skipped"

# OURS: the pool as this worker finds it (swf-monitor docs/POOL_REPORTER.md,
# Pools we cannot read). squeue and sinfo answer on the compute node
# outside the container and nowhere inside it, so the sample is taken
# here, once per worker, by the first task only, and written above the
# tasks' working directories as pool-sample.json. /pscratch is mounted
# at its own path in both containers, so the payload reads the file at
# the path the environment file names (EPICPROD_POOL_SAMPLE, carried
# into the payload's container by its APPTAINERENV_ copy below) and
# carries it in its report. A sample that cannot be taken is recorded
# as such, never a failure.
POOL_SAMPLE=$ACCESS_POINT/${SLURM_JOBID:-nojob}/pool-sample.json
if [[ ${SLURM_PROCID:-0} == 0 ]]; then
    PART=${SLURM_JOB_PARTITION:-}
    ACCT=${SLURM_JOB_ACCOUNT:-}
    squeue -h -o '%T %D %a %V' > squeue.txt 2> squeue.err; SQ_RC=$?
    sinfo -h ${PART:+-p "$PART"} -o '%T %D' > sinfo.txt 2> sinfo.err; SI_RC=$?
    python3 - "$SQ_RC" "$SI_RC" "$ACCT" "$PART" > "$POOL_SAMPLE" <<'PY'
import json, socket, sys
from datetime import datetime, timezone
sq_rc, si_rc, acct, part = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3], sys.argv[4]
now = datetime.now(timezone.utc)
out = {"schema": "pool-sample/1", "taken_at": now.isoformat(timespec="seconds"),
       "host": socket.gethostname(), "cluster": "perlmutter", "partition": part,
       "account": acct, "source": "squeue and sinfo on the worker node", "errors": []}
def bucket(state):
    return {"RUNNING": "running", "PENDING": "pending"}.get(state, "other")
machine = {"running_jobs": 0, "running_nodes": 0, "pending_jobs": 0, "pending_nodes": 0, "other_jobs": 0}
ours = dict(machine, oldest_pending_s=None)
oldest = None
if sq_rc == 0:
    for line in open("squeue.txt"):
        f = line.split()
        if len(f) < 3:
            continue
        b = bucket(f[0])
        try:
            nodes = int(f[1])
        except ValueError:
            nodes = 0
        for d in ((machine, True), (ours, f[2] == acct)):
            if not d[1]:
                continue
            d[0][b + "_jobs"] += 1
            if b != "other":
                d[0][b + "_nodes"] += nodes
        if b == "pending" and f[2] == acct and len(f) > 3:
            try:
                t = datetime.strptime(f[3], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
                oldest = t if oldest is None or t < oldest else oldest
            except ValueError:
                pass
    if oldest is not None:
        ours["oldest_pending_s"] = int((now - oldest).total_seconds())
else:
    out["errors"].append("squeue exited %d: %s" % (sq_rc, open("squeue.err").read().strip()[:200]))
nodes = {}
if si_rc == 0:
    for line in open("sinfo.txt"):
        f = line.split()
        if len(f) == 2:
            try:
                nodes[f[0].rstrip("*~#!%$@^-").lower()] = nodes.get(f[0].rstrip("*~#!%$@^-").lower(), 0) + int(f[1])
            except ValueError:
                pass
else:
    out["errors"].append("sinfo exited %d: %s" % (si_rc, open("sinfo.err").read().strip()[:200]))
out["machine"] = machine
out["ours"] = ours
out["partition_nodes"] = dict(nodes, total=sum(nodes.values()))
print(json.dumps(out))
PY
    log "pool sample: $(cat "$POOL_SAMPLE" 2>/dev/null || echo none)"
    rm -f squeue.txt squeue.err sinfo.txt sinfo.err
fi

# OURS: the node guard's exclusion (site-canary docs/NODE_GUARD.md,
# Actuation): the document the guard publishes beside the pilot, fetched
# once. A live document in force that lists this host on this queue ends
# the launch here, before a pilot fetches a job; anything else, a shadow
# document, a fetch that fails, a document that does not parse, proceeds.
NODE_EXCLUSION_URL=${NODE_EXCLUSION_URL:-https://epic-devcloud-stageout.s3.us-east-1.amazonaws.com/pilot/node-exclusion.json}
EXCLUSION_DOC=$(curl -sfL -m 10 "$NODE_EXCLUSION_URL" 2>/dev/null)
EXCLUDED=$(python3 - "$PQ" "$EXCLUSION_DOC" <<'PY' 2>/dev/null
import json, socket, sys
from datetime import datetime, timezone
queue = sys.argv[1]
try:
    d = json.loads(sys.argv[2])
    until = datetime.fromisoformat(str(d.get("valid_until", "")).replace("Z", "+00:00"))
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    if d.get("mode") == "live" and datetime.now(timezone.utc) <= until:
        names = set()
        for n in (socket.gethostname(), socket.getfqdn()):
            n = (n or "").strip().lower()
            names.update({n, n.split(".", 1)[0]})
        bare = {x.split(".", 1)[0] for x in names}
        for e in d.get("nodes") or []:
            h = str(e.get("host") or "").strip().lower()
            if h and (not e.get("queue") or e["queue"] == queue) and (h in names or ("." not in h and h in bare)):
                print(f'{h} on {e.get("queue")} since {e.get("since")} ({e.get("reason")})')
                break
except Exception:
    pass
PY
)
if [[ -n "$EXCLUDED" ]]; then log "node guard excludes this node: $EXCLUDED; no pilot started"; exit 0; fi

# Stage the bounded Perlmutter batch runtime. Perlmutter compute nodes
# cannot read eic.opensciencegrid.org, so immutable HTTPS objects carry it and
# the matching Python 3.13 ES channel. Fail closed; never fall back to ALRB or
# a different pilot after a failed download or verification.
fetch() {
    curl -sfL --retry 2 --connect-timeout 15 -m 90 "$1" -o "$2"
}
verify() {
    echo "$1  $2" | sha256sum -c --quiet
}
fetch "$RUNTIME_URL" native-runtime.tar.gz \
    && verify "$RUNTIME_SHA256" native-runtime.tar.gz \
    && mkdir -p native-runtime \
    && tar -xzf native-runtime.tar.gz -C native-runtime \
    || { log 'native runtime staging failed; no pilot started'; exit 65; }
python3 - native-runtime "$PILOT_SHA256" <<'CHECK' \
    || { log 'native runtime manifest failed; no pilot started'; exit 65; }
import hashlib, json, sys
from pathlib import Path
root = Path(sys.argv[1])
manifest = json.loads((root / 'manifest.json').read_text())
assert manifest['pilot_archive_sha256'] == sys.argv[2], 'wrong pilot archive'
assert manifest.get('es_batch_channel', {}).get('version') == 1, 'batch channel missing'
assert manifest['es_batch_channel']['stageout_s'] == 300, 'wrong reporting cadence'
for name, expected in manifest['files'].items():
    assert hashlib.sha256((root / name).read_bytes()).hexdigest() == expected, name
CHECK
fetch "$ES_CHANNEL_URL" es-channel.tar.gz \
    && verify "$ES_CHANNEL_SHA256" es-channel.tar.gz \
    && tar -xzf es-channel.tar.gz \
    && [[ -r es-channel/python/yampl.cpython-313-x86_64-linux-gnu.so ]] \
    || { log 'Python 3.13 ES channel staging failed; no pilot started'; exit 65; }
fetch "$QUEUE_URL/queuedata.json" queuedata.json \
    && fetch "$QUEUE_URL/ddmendpoints.json" ddmendpoints.json \
    || { log 'queue/storage configuration staging failed; no pilot started'; exit 65; }

# Preserve the site's Harvester identity. A private local proxy copy remains
# readable at the same absolute path inside the campaign image.
umask 077
cp "$HARVESTER_DIR/globus/harvesterproxy" harvesterproxy \
    && chmod 600 harvesterproxy \
    || { log 'Harvester proxy unavailable; no pilot started'; exit 67; }
export X509_USER_PROXY=$WORKDIR/harvesterproxy
export EPICPROD_NATIVE_PROXY=harvesterproxy
export EPICPROD_NATIVE_BATCH=1
export EPICPROD_NATIVE_ASSETS=$WORKDIR/native-runtime
export EPICPROD_NATIVE_ARCHIVE=pilot3-native.tar.gz
export EPICPROD_NATIVE_IMAGE=$IMAGE
export EPICPROD_ES_CHANNEL=$WORKDIR/es-channel
export PILOT_ES_EXECUTOR_TYPE=generic
export EPICPROD_SWITCH_ES_BATCH_CHANNEL=1
export EPICPROD_ES_STAGEOUT_GAP=300
# The job's ranges held on the node from its start, 200,000 events of them,
# refilled straight from the server below half (about seven minutes of a fed
# node), and each close credited as it stands, not at the stage-out gap
# (es_batch.py). The size is in events whatever the range size.
export EPICPROD_ES_PREFETCH_EVENTS=200000
export EPICPROD_ES_REPORT_AT_ONCE=1
export STORAGEDATA_SERVER_URL=file://$WORKDIR/ddmendpoints.json
export EPICPROD_POOL_SAMPLE=$POOL_SAMPLE
export APPTAINERENV_EPICPROD_POOL_SAMPLE=$POOL_SAMPLE
# One payload thread per physical core; Slurm exposes 256 hardware threads.
export ATHENA_PROC_NUMBER=128
export APPTAINER_BINDPATH=${APPTAINER_BINDPATH:+$APPTAINER_BINDPATH,}/global,/pscratch
export MACHINEFEATURES=$WORKDIR/sitefeatures
mkdir -p "$MACHINEFEATURES"
printf '%s\n' "$DEADLINE_AT" > "$MACHINEFEATURES/shutdowntime"
export EPICPROD_MIN_TIME_FOR_NEW_JOB=600
SECONDS_LEFT=$(( DEADLINE_AT - $(date +%s) ))
(( SECONDS_LEFT > EPICPROD_MIN_TIME_FOR_NEW_JOB )) \
    || { log 'commissioning worker has insufficient time left; no pilot started'; exit 73; }

native_pid=
forward_host() {
    if [[ -n "$native_pid" ]]; then
        log "signal $1 received, forwarding to native pilot process $native_pid"
        kill -s "$1" "$native_pid" 2>/dev/null || true
        wait "$native_pid"; rc=$?
        exit "$rc"
    fi
    log "signal $1 received before the native pilot started"; exit 1
}
for sig in INT QUIT USR1 TERM CONT XCPU; do trap "forward_host $sig" "$sig"; done
log "native runtime $RUNTIME_SHA256; pilot $PILOT_SHA256; Python 3.13 channel $ES_CHANNEL_SHA256"
# Preserve the scheduler's real identity before entering the common launcher's
# private mount namespace. The CVMFS Apptainer cannot resolve Perlmutter's NSS
# user there. Bind local lookup files in this process namespace only; neither
# the host account files nor the uid/gid change.
uid=$(id -u); gid=$(id -g)
passwd_entry=$(getent passwd "$uid") \
    || { log 'scheduler user lookup failed; no pilot started'; exit 67; }
group_entry=$(getent group "$gid") \
    || { log 'scheduler group lookup failed; no pilot started'; exit 67; }
cp /etc/passwd native-passwd && cp /etc/group native-group \
    || { log 'private identity staging failed; no pilot started'; exit 67; }
if ! awk -F: -v uid="$uid" '$3 == uid {found=1} END {exit !found}' native-passwd; then
    printf '%s\n' "$passwd_entry" >> native-passwd
fi
if ! awk -F: -v gid="$gid" '$3 == gid {found=1} END {exit !found}' native-group; then
    printf '%s\n' "$group_entry" >> native-group
fi
log "staged scheduler identity uid=$uid gid=$gid for private container lookup"
log "starting common native pilot in $IMAGE; ${SECONDS_LEFT}s left; one job maximum"
# The public startup record contains only the adapter's own messages. Pilot
# console and job diagnostics stay in the private worker file and normal
# pilot-log route; never publish an environment or unfiltered debug output.
timeout --signal=TERM --kill-after=60s "$SECONDS_LEFT" \
    unshare --user --map-current-user --keep-caps --mount --propagation private -- \
    /bin/bash -ceu 'mount --bind "$1" /etc/passwd; mount --bind "$2" /etc/group; shift 2; exec "$@"' \
    native-site-identity "$WORKDIR/native-passwd" "$WORKDIR/native-group" \
    bash "$EPICPROD_NATIVE_ASSETS/epic-native-pilot.sh" \
    -q "$PQ" -i PR -j managed -w generic \
    --url https://pandaserver01.sdcc.bnl.gov -p 25443 \
    --queuedata-url "file://$WORKDIR/queuedata.json" \
    --storagedata-url "file://$WORKDIR/ddmendpoints.json" \
    --pilot-user epic --allow-same-user=True --job-type=managed \
    --use-rucio-traces False --rucio-host https://nprucio01.sdcc.bnl.gov:443 \
    --getjobrequests=1 --getjobfailures=1 --cleanup True --noworkerpilotstatusupdate \
    > native-pilot-console.log 2>&1 &
native_pid=$!
wait "$native_pid"; rc=$?
if (( rc != 0 )); then
    # Only the common launcher's startup markers are safe in the public
    # record; the complete console remains private.
    sed -r 's/\x1B\[[0-9;]*[[:alpha:]]//g' native-pilot-console.log \
        | grep -E '^ePIC native pilot(:| startup failed)|^ModuleNotFoundError:|^(FATAL|ERROR|WARNING)[ :]|^(unshare|mount|apptainer):' \
        | tail -12 || true
fi
# The pilot's steps between the payload's end and its own exit, which decide
# how long the node stays allocated after the work (docs/NERSC_PERLMUTTER.md,
# The end of a job): selected timestamped step lines, nothing credential-like.
log "native pilot end of job:"
sed -r 's/\x1B\[[0-9;]*[[:alpha:]]//g' native-pilot-console.log \
    | grep -E '^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9:,]+ \|' \
    | grep -E 'ESProcess finished|receipt archive|final report|tar/zip event ranges|stag(ing|ed) output file|failed to stage out|ES generic executor finished|final server update|this job has now completed|report files in Harvester mode|proceeding with|thread has finished|clean|work dir|pilot has finished|exit code' \
    | grep -viE 'token|secret|passw|authori|bearer|x509|proxy|cert|pre-merge' \
    | sed -r 's/[A-Za-z0-9+\/=_-]{40,}/<long value>/g' | cut -c1-200 | tail -60 || true
log "native pilot finished with code $rc"
exit "$rc"
