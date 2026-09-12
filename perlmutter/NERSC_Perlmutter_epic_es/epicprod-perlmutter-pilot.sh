#!/bin/bash
# The epicprod pilot start on Perlmutter for NERSC_Perlmutter_epic_es,
# the Event Service test queue (docs/NERSC_PERLMUTTER.md). The site's
# Slurm job fetches this file by queue name at worker start and runs one
# copy per pilot:
#
#   srun -n <tasks> --export=HARVESTER_ID,HARVESTER_WORKER_ID,GTAG \
#        /bin/bash ./epicprod-perlmutter-pilot.sh <queue> <accessPoint>
#
# What it does, per task: a working directory under the access point,
# the pilot from the tarball named below (checksum verified), the
# queue's pilot-side configuration from this directory of the
# repository, the Event Service executor setting, then the pilot inside
# the AlmaLinux 9 container that ALRB provides from /cvmfs/atlas.cern.ch,
# with Slurm's signals forwarded to the pilot so it can clean up at the
# wall. The pilot's Python, Rucio, XRootD, davix, psutil, logstash and
# prmon come from ALRB (lsetup).
set -u

PQ=${1:?panda queue}
ACCESS_POINT=${2:?harvester access point}

# What production operations publishes for this queue.
QUEUE_URL=https://raw.githubusercontent.com/BNLNPPS/swf-epicprod/main/perlmutter/$PQ
PILOT_TARBALL_URL=https://github.com/BNLNPPS/swf-epicprod/releases/download/pilot-3.14.3.3-epic3/pilot3-3.14.3.3-epic3.tar.gz
PILOT_TARBALL_SHA256=78bebbc4031b041b3ef0ef99fa25f465fb106a0d4b0311f1f998efc025bb9adb
# The Event Service channel library (python-yampl) built for the
# container's Python, as a tarball unpacked into the working directory;
# empty until one is published, in which case only ordinary jobs run.
ES_CHANNEL_URL=

# The site's harvester installation: the Rucio client configuration for
# the pilot's Rucio account lives there.
HARVESTER_DIR=/global/common/software/m3763/panda-harvester

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }

WORKDIR=$ACCESS_POINT/${SLURM_JOBID:-nojob}/${SLURM_PROCID:-0}
mkdir -p "$WORKDIR" && cd "$WORKDIR" || { log "cannot enter $WORKDIR"; exit 1; }
log "queue $PQ worker ${HARVESTER_WORKER_ID:-?} harvester ${HARVESTER_ID:-?} task ${SLURM_PROCID:-0} host $(hostname -s) workdir $PWD"
unset TMPDIR

# The pilot.
curl -sfL "$PILOT_TARBALL_URL" -o pilot3.tar.gz || { log "pilot tarball fetch failed: $PILOT_TARBALL_URL"; exit 1; }
echo "$PILOT_TARBALL_SHA256  pilot3.tar.gz" | sha256sum -c --quiet || { log "pilot tarball checksum mismatch"; exit 1; }
tar -xzf pilot3.tar.gz || { log "pilot tarball unpack failed"; exit 1; }
[[ -f pilot3/pilot.py ]] || { log "no pilot3/pilot.py in the tarball"; exit 1; }
log "pilot $(cat pilot3/PILOTVERSION 2>/dev/null || echo unknown) from $PILOT_TARBALL_URL"

# The queue's pilot-side configuration: the ePIC pilot reads
# ./queuedata.json before the server cache. Absent, the cache applies.
if curl -sfL "$QUEUE_URL/queuedata.json" -o queuedata.json; then
    log "queuedata.json from $QUEUE_URL"
else
    rm -f queuedata.json
    log "no queuedata.json published for $PQ; the pilot uses the server cache"
fi

# The Event Service channel library, when published.
ES_PYTHONPATH=
if [[ -n "$ES_CHANNEL_URL" ]]; then
    if curl -sfL "$ES_CHANNEL_URL" -o es-channel.tar.gz && tar -xzf es-channel.tar.gz; then
        ES_PYTHONPATH=$PWD/es-channel/python
        log "event service channel library from $ES_CHANNEL_URL"
    else
        log "event service channel library fetch failed: $ES_CHANNEL_URL"
    fi
fi

# The container's environment file (ALRB sources it inside the container).
cat > myEnv.sh <<EOF
export HARVESTER_ID=${HARVESTER_ID:-}
export HARVESTER_WORKER_ID=${HARVESTER_WORKER_ID:-}
export GTAG=${GTAG:-}
lsetup -q "python pilot-default-SL9"
lsetup -q rucio xrootd davix psutil logstash prmon
export RUCIO_ACCOUNT=panda
export RUCIO_CONFIG=$HARVESTER_DIR/rucio.cfg
export X509_VOMS_DIR=/cvmfs/oasis.opensciencegrid.org/mis/vodata/grid-security/vomsdir
export X509_VOMSES=/cvmfs/oasis.opensciencegrid.org/mis/vodata/vomses
export X509_CERT_DIR=/cvmfs/oasis.opensciencegrid.org/mis/certificates
export PILOT_ES_EXECUTOR_TYPE=generic
export PANDA_QUEUE=$PQ
EOF
[[ -n "$ES_PYTHONPATH" ]] && echo "export PYTHONPATH=$ES_PYTHONPATH\${PYTHONPATH:+:\$PYTHONPATH}" >> myEnv.sh

# The payload script (runs inside the container): the pilot in the
# background, Slurm's signals forwarded to it.
cat > myPayload.sh <<'EOF'
#!/bin/bash
plog() { echo "$(date -u +'%Y-%m-%d %H:%M:%S') [payload] $*"; }
pilot_pid=
forward() {
    if [[ -n "$pilot_pid" ]]; then
        plog "signal $1 received, forwarding to pilot $pilot_pid"
        kill -s "$1" "$pilot_pid" 2>/dev/null
        wait "$pilot_pid"; rc=$?
        plog "pilot exited with code $rc"; exit $rc
    fi
    plog "signal $1 received before the pilot started"; exit 1
}
for s in INT QUIT USR1 TERM CONT XCPU; do trap "forward $s" $s; done
cd /srv
plog "starting the pilot"
python3 pilot3/pilot.py -q "$PANDA_QUEUE" -i PR -j managed -w generic \
    --url https://pandaserver01.sdcc.bnl.gov -p 25443 \
    --queuedata-url "http://pandaserver01.sdcc.bnl.gov:25080/cache/schedconfig/$PANDA_QUEUE.all.json" \
    --pilot-user epic --allow-same-user=False --job-type=managed \
    --use-rucio-traces False --rucio-host https://nprucio01.sdcc.bnl.gov:443 \
    --getjobrequests=50 --cleanup True --noworkerpilotstatusupdate --debug &
pilot_pid=$!
wait "$pilot_pid"; rc=$?
plog "pilot completed with return code $rc"
exit $rc
EOF
chmod +x myPayload.sh

# The container: ALRB's AlmaLinux 9 image with /global and /pscratch
# mounted (/cvmfs by default) and this directory as /srv. Its scratch
# home lives under the access point, not in a personal area.
export ATLAS_LOCAL_ROOT_BASE=/cvmfs/atlas.cern.ch/repo/ATLASLocalRootBase
export ALRB_CONT_CHOME=$ACCESS_POINT/.alrb/container/apptainer
export ALRB_CONT_RUNPAYLOAD="source /srv/myPayload.sh"
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
# out of this script's own environment.
( source "$ATLAS_LOCAL_ROOT_BASE/user/atlasLocalSetup.sh" -c el9 -s /srv/myEnv.sh -m /global -m /pscratch ) &
setup_pid=$!
wait "$setup_pid"; rc=$?
log "container finished with code $rc"
exit $rc
