#!/bin/bash
# The epicprod pilot launch on Perlmutter for NERSC_Perlmutter_epic_es
# (docs/NERSC_PERLMUTTER.md). The site's Slurm job fetches this file by
# queue name at worker start and runs one copy per pilot:
#
#   srun -n <tasks> --export=HARVESTER_ID,HARVESTER_WORKER_ID,GTAG \
#        /bin/bash ./epicprod-perlmutter-pilot-launch.sh <queue> <accessPoint>
#
# Its nucleus is the site's own production wrapper
# (wrapper-wrapper-3-epic-test.sh, PanDA operations' file), reproduced
# from the public worker record of production job 3102135 (worker
# 20721, 2026-09-19): the same working directory, the same environment
# file and payload script inside the container, byte for byte, the same
# pilot from the harvester installation, the same container from ALRB
# with the same mounts, the same signal forwarding. What is ours is
# marked: the pool sample and the node guard's exclusion, both before
# the container starts. The Event Service material the first version of
# this file carried (a pilot from the devcloud bucket, queuedata.json,
# the yampl channel) is in this file's history (swf-epicprod b24baf1)
# and returns as an evolution step once this nucleus has run.
set -u

PQ=${1:?panda queue}
ACCESS_POINT=${2:?harvester access point}

# The site's harvester installation: the pilot release the site runs,
# and the Rucio client configuration for the pilot's Rucio account.
HARVESTER_DIR=/global/common/software/m3763/panda-harvester
# The site wrapper's latestNERSCPilotVer on 2026-09-19.
NERSC_PILOT_VERSION=3.14.3.3
PILOT_PY=$HARVESTER_DIR/pilot/pilot3-$NERSC_PILOT_VERSION/pilot3/pilot.py

# OURS, the Event Service step (docs/NODE_EVENT_DISPATCHER.md): the
# pilot from the devcloud bucket, the site's release with the two
# event-service fixes (pilot3 PRs 220 and 221), pinned by checksum; the
# queue's pilot-side configuration with the es_events activities; and
# yampl, the library the generic executor hands ranges through, built
# for the container's Python. Each is fetched into this directory,
# which is /srv inside the container. A fetch that fails leaves the
# site's own pilot and configuration in place, so ordinary jobs run.
QUEUE_URL=https://raw.githubusercontent.com/BNLNPPS/swf-epicprod/main/perlmutter/$PQ
PILOT_TARBALL_URL=https://epic-devcloud-stageout.s3.us-east-1.amazonaws.com/pilot/pilot3-3.14.3.3-epic3.tar.gz
PILOT_TARBALL_SHA256=b7b0e27141a9d6f6b7e4fb9a2c3dea91bc5669f90aa39c3b360c9ad6b6e02721
ES_CHANNEL_URL=https://epic-devcloud-stageout.s3.us-east-1.amazonaws.com/pilot/es-channel-py311-el9.tar.gz
ES_CHANNEL_SHA256=f6c11690f046ae8ceb90886d034e4fbba0f494203f839a16e8a85f687efcc938

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
[[ -f $PILOT_PY ]] || { log "no pilot at $PILOT_PY"; exit 1; }
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

# OURS: the Event Service step's material (above), fetched by every
# task; the pilot runs from /srv/pilot3 when ours arrived whole.
if curl -sfL -m 60 "$PILOT_TARBALL_URL" -o pilot3.tar.gz \
    && echo "$PILOT_TARBALL_SHA256  pilot3.tar.gz" | sha256sum -c --quiet \
    && tar -xzf pilot3.tar.gz && [[ -f pilot3/pilot.py ]]; then
    PILOT_PY=/srv/pilot3/pilot.py
    log "pilot $(cat pilot3/PILOTVERSION 2>/dev/null || echo unknown)-epic3 from $PILOT_TARBALL_URL"
else
    rm -rf pilot3 pilot3.tar.gz
    log "our pilot not usable (fetch, checksum or unpack failed): $PILOT_TARBALL_URL; the site's pilot runs"
fi
if curl -sfL -m 30 "$QUEUE_URL/queuedata.json" -o queuedata.json; then
    log "queuedata.json from $QUEUE_URL"
else
    rm -f queuedata.json
    log "no queuedata.json published for $PQ; the pilot uses the server cache"
fi
ES_PYTHONPATH=
if curl -sfL -m 60 "$ES_CHANNEL_URL" -o es-channel.tar.gz \
    && echo "$ES_CHANNEL_SHA256  es-channel.tar.gz" | sha256sum -c --quiet \
    && tar -xzf es-channel.tar.gz; then
    ES_PYTHONPATH=/srv/es-channel/python
    log "event service channel library from $ES_CHANNEL_URL"
else
    rm -rf es-channel es-channel.tar.gz
    log "event service channel library not usable (fetch, checksum or unpack failed): $ES_CHANNEL_URL"
fi

# The container's environment file, as the site wrapper writes it (ALRB
# sources it inside the container).
cat > myEnv.sh <<EOF
# Created on $(date)
export HARVESTER_ID=${HARVESTER_ID:-}
export HARVESTER_WORKER_ID=${HARVESTER_WORKER_ID:-}
export GTAG=${GTAG:-}
lsetup -q "python pilot-default-SL9"
export RUCIO_ACCOUNT=panda
lsetup -q rucio xrootd davix psutil logstash prmon
export ALRB_CONT_CHOME=/pscratch/sd/x/xin/panda/.alrb/container/apptainer
export RUCIO_CONFIG=$HARVESTER_DIR/rucio.cfg
export X509_VOMS_DIR=/cvmfs/oasis.opensciencegrid.org/mis/vodata/grid-security/vomsdir
export X509_VOMSES=/cvmfs/oasis.opensciencegrid.org/mis/vodata/vomses
export X509_CERT_DIR=/cvmfs/oasis.opensciencegrid.org/mis/certificates
# OURS: where the pool sample is (this worker's, taken by its first task).
# The payload runs in a second container the pilot starts with a clean
# environment (-C), so the plain variable stops at the pilot (worker
# 21108, job 3492628: no pool block); apptainer injects APPTAINERENV_-
# prefixed variables through a clean environment, the route ALRB uses
# for its own ENV, and /pscratch is bound in that container.
export EPICPROD_POOL_SAMPLE=$POOL_SAMPLE
export APPTAINERENV_EPICPROD_POOL_SAMPLE=$POOL_SAMPLE
# OURS: the Event Service executor and its channel library (the pilot
# runs an event-service payload in this environment, not in the job's
# container, so the payload sees them too).
export PILOT_ES_EXECUTOR_TYPE=generic
EOF
[[ -n "$ES_PYTHONPATH" ]] && echo "export PYTHONPATH=$ES_PYTHONPATH\${PYTHONPATH:+:\$PYTHONPATH}" >> myEnv.sh

# The payload script (runs inside the container), as the site wrapper
# writes it: the pilot in the background, Slurm's signals forwarded to
# it. The queue name and the pilot path are the two substitutions.
cat > myPayload.sh <<EOF
#!/bin/bash
# Created on \$(date)
# This script runs inside the container and handles signals
function log_payload() {
  dt=\$(date --utc +"%Y-%m-%d %H:%M:%S,%3N [payload]")
  echo "\$dt \$@"
}
function err_payload() {
  dt=\$(date --utc +"%Y-%m-%d %H:%M:%S,%3N [payload]")
  echo "\$dt \$@" >&2
}
function payload_trap_handler() {
  if [[ -n "\${pilot_pid}" ]]; then
    log_payload "Signal \$1 received, forwarding to pilot PID: \$pilot_pid"
    err_payload "Signal \$1 received, forwarding to pilot PID: \$pilot_pid"
    # Forward signal to pilot
    kill -s "\$1" "\$pilot_pid" 2>/dev/null
    # Wait for pilot to finish cleanup
    wait "\$pilot_pid" 2>/dev/null
    pilot_rc=\$?
    log_payload "Pilot exited with code: \$pilot_rc"
    exit \$pilot_rc
  else
    log_payload "Signal \$1 received before pilot started"
    err_payload "Signal \$1 received before pilot started"
    exit 1
  fi
}
# Set up signal traps inside container
trap 'payload_trap_handler 2' SIGINT
trap 'payload_trap_handler 3' SIGQUIT
trap 'payload_trap_handler 10' SIGUSR1
trap 'payload_trap_handler 15' SIGTERM
trap 'payload_trap_handler 18' SIGCONT
trap 'payload_trap_handler 24' SIGXCPU
log_payload "Starting payload script inside container"
env | sort
# Start pilot in background so we can handle signals
python3 $PILOT_PY -q $PQ -i PR -j managed -w generic --url https://pandaserver01.sdcc.bnl.gov -p 25443 --queuedata-url http://pandaserver01.sdcc.bnl.gov:25080/cache/schedconfig/$PQ.all.json --pilot-user epic --allow-same-user=False --job-type=managed --use-rucio-traces False --rucio-host https://nprucio01.sdcc.bnl.gov:443 --getjobrequests=50 --cleanup True   --noworkerpilotstatusupdate --debug &
pilot_pid=\$!
log_payload "Pilot started with PID: \$pilot_pid"
# Wait for pilot to complete
wait "\$pilot_pid"
pilot_rc=\$?
log_payload "Pilot completed with return code: \$pilot_rc"
exit \$pilot_rc
EOF
chmod +x myPayload.sh

# The container, as the site wrapper starts it: ALRB's AlmaLinux 9 image
# with /global and /pscratch mounted (/cvmfs by default), this directory
# as /srv and the working directory inside, the environment file sourced
# and the payload script run. setupATLAS runs in the background so that
# Slurm's signals reach it and, through the payload script, the pilot.
export ATLAS_LOCAL_ROOT_BASE=/cvmfs/atlas.cern.ch/repo/ATLASLocalRootBase
export ALRB_CONT_CHOME=/pscratch/sd/x/xin/panda/.alrb/container/apptainer
# The harvester's proxy, as the site wrapper exports it on the host: ALRB
# copies the file X509_USER_PROXY names into the container's dummy home
# as /alrb/harvesterproxy and points the variable there, which is how the
# pilot finds its credential. Without it the pilot reports "SSL
# communication is impossible" and never asks for a job (worker 21107).
export X509_USER_PROXY=$HARVESTER_DIR/globus/harvesterproxy
export ALRB_CONT_SETUPFILE=/srv/myEnv.sh
export ALRB_CONT_RUNPAYLOAD=/srv/myPayload.sh
mkdir -p "$ALRB_CONT_CHOME"

setup_pid=
forward_host() {
    if [[ -n "$setup_pid" ]]; then
        log "signal $1 received, forwarding to the container process $setup_pid"
        kill -s "$1" "$setup_pid" 2>/dev/null
        wait "$setup_pid"; rc=$?
        log "container process exited with code $rc"; exit $rc
    fi
    log "signal $1 received before the container started"; exit 1
}
for s in INT QUIT USR1 TERM CONT XCPU; do trap "forward_host $s" $s; done

log "starting the container"
# setupATLAS is this source line; the subshell keeps the sourced setup
# out of this script's own environment, and drops set -u, under which
# ALRB's setup fails on its own unbound variables.
# The setup file and the payload script travel in ALRB_CONT_SETUPFILE and
# ALRB_CONT_RUNPAYLOAD, exported above as the site wrapper exports them;
# the two mounts give ALRB's option string the site's exact form.
( set +u; source "$ATLAS_LOCAL_ROOT_BASE/user/atlasLocalSetup.sh" -c el9 -m /global -m /pscratch ) &
setup_pid=$!
wait "$setup_pid"; rc=$?
log "container finished with code $rc"
exit $rc
