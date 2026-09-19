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

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }

WORKDIR=$ACCESS_POINT/${SLURM_JOBID:-nojob}/${SLURM_PROCID:-0}
mkdir -p "$WORKDIR" && cd "$WORKDIR" || { log "cannot enter $WORKDIR"; exit 1; }
log "queue $PQ worker ${HARVESTER_WORKER_ID:-?} harvester ${HARVESTER_ID:-?} task ${SLURM_PROCID:-0} host $(hostname -s) workdir $PWD"
unset TMPDIR
[[ -f $PILOT_PY ]] || { log "no pilot at $PILOT_PY"; exit 1; }

# OURS: the pool as this worker finds it (swf-monitor docs/POOL_REPORTER.md,
# Pools we cannot read). squeue and sinfo answer on the compute node
# outside the container and nowhere inside it, so the sample is taken
# here, once per worker, by the first task only, and written above the
# tasks' working directories as pool-sample.json. /pscratch is mounted
# at its own path in the container, so the payload reads the file at
# the path the environment file names (EPICPROD_POOL_SAMPLE) and carries
# it in its report. A sample that cannot be taken is recorded as such,
# never a failure.
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
export EPICPROD_POOL_SAMPLE=$POOL_SAMPLE
EOF

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
( set +u; source "$ATLAS_LOCAL_ROOT_BASE/user/atlasLocalSetup.sh" -c el9 -s /srv/myEnv.sh -r /srv/myPayload.sh --pwd /srv -m /global -m /pscratch ) &
setup_pid=$!
wait "$setup_pid"; rc=$?
log "container finished with code $rc"
exit $rc
