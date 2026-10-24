#!/bin/bash
# ePIC native pilot launcher for the OSG pool: pilot3 runs with the campaign
# image's own Python, inside the image the glidein was asked for with
# +SingularityImage. No ATLAS bootstrap. A job for that image runs in place; a
# job for another image runs nested in its own image (native_container.py).
#   Environment: EPICPROD_NATIVE_IMAGE   the image +SingularityImage names
#                EPICPROD_NATIVE_ARCHIVE the transferred pilot archive
#                EPICPROD_NATIVE_PREFLIGHT_IMAGE  another campaign image: a trivial
#                  nested execution in it must pass before any job is acquired
#   Arguments: pilot.py's, unchanged.
# Nested launches run the OSG pool's Apptainer on CVMFS through its wrapper
# inside a private namespace that carries the session directory the runtime
# falls back to; the preflight proves that route before any job is taken.
# docs/NATIVE_PILOT_CANARY.md § Staged trial, 23 October.
# On a batch allocation with no glidein (GREX through its CE), with
# EPICPROD_NATIVE_BATCH=1, the launcher starts the image once itself through
# Apptainer and runs inside it; the rest is unchanged. The shipped files may be
# in the allocation's directory (EPICPROD_NATIVE_ASSETS, set by
# epicprod-multipilot.sh for its pilots) and are linked into this pilot's; a
# shipped proxy named by EPICPROD_NATIVE_PROXY wins over a CE's rewritten
# X509_USER_PROXY; a queuedata.json here becomes pilot.py's queue data unless
# the arguments name one. docs/NATIVE_PILOT_CANARY.md § GREX.
set -euo pipefail
trap 'echo "ePIC native pilot startup failed at line $LINENO (exit $?)" >&2' ERR
IMAGE=${EPICPROD_NATIVE_IMAGE:?campaign image required}
ARCHIVE=${EPICPROD_NATIVE_ARCHIVE:?transferred pilot archive required}
HERE=$(pwd -P)
SELF=$(readlink -f "$0")
ASSETS=$(readlink -f "${EPICPROD_NATIVE_ASSETS:-$HERE}")
if [[ $ASSETS != "$HERE" ]]; then
    for f in "$ARCHIVE" certificates.tar.gz rucio.cfg glidein-deadline.sh job-user.sh \
             runtime-policy.py queue-runtime-policy.json ${EPICPROD_NATIVE_PROXY:-}; do
        [[ -e $ASSETS/$f && ! -e $HERE/$f ]] && ln -s "$ASSETS/$f" "$HERE/$f"
    done
fi
if [[ -n ${EPICPROD_NATIVE_PROXY:-} && -r $HERE/$EPICPROD_NATIVE_PROXY ]]; then
    export X509_USER_PROXY=$HERE/$EPICPROD_NATIVE_PROXY
fi
ACTUAL=${APPTAINER_CONTAINER:-${SINGULARITY_CONTAINER:-}}
if [[ -z $ACTUAL && ${EPICPROD_NATIVE_BATCH:-0} == 1 ]]; then
    # The node's runtime, else the OSG pool's on CVMFS through its wrapper (which
    # supplies its libraries; the raw binary has lacked libsubid.so.3), else
    # the raw binary, which the 22 October GREX canary ran.
    RUNTIME=$(command -v apptainer || command -v singularity || true)
    for candidate in /cvmfs/oasis.opensciencegrid.org/mis/apptainer/1.4.5/bin/apptainer \
                     "/cvmfs/oasis.opensciencegrid.org/mis/apptainer/1.4.5/$(uname -m)/bin/apptainer"; do
        [[ -n $RUNTIME ]] && break
        [[ -x $candidate ]] && RUNTIME=$candidate
    done
    [[ -x $RUNTIME ]] || { echo "Apptainer unavailable: $RUNTIME" >&2; exit 69; }
    binds=(--bind /cvmfs:/cvmfs --bind "$HERE:$HERE" --bind "$ASSETS:$ASSETS" --bind "$SELF:$SELF")
    for d in "${SLURM_TMPDIR:-}" "/scratch/slurm/${SLURM_JOB_ID:-}" "${_CONDOR_SCRATCH_DIR:-}" \
             "$(dirname "$(readlink -f "${X509_USER_PROXY:-/nonexistent}")")"; do
        [[ -n ${SLURM_JOB_ID:-} || $d != /scratch/slurm/ ]] || continue
        [[ -d $d && $d != / ]] && binds+=(--bind "$d:$d")
    done
    echo "ePIC native pilot: batch host $(hostname), runtime $RUNTIME, image $IMAGE"
    exec "$RUNTIME" exec --no-home "${binds[@]}" --pwd "$HERE" "$IMAGE" /bin/bash "$SELF" "$@"
fi
[[ -n $ACTUAL ]] || { echo "not inside a container: the slot ignored +SingularityImage $IMAGE" >&2; exit 69; }
[[ $(readlink -f "$ACTUAL") == $(readlink -f "$IMAGE") ]] || {
    echo "outer image $ACTUAL is not $IMAGE; refusing" >&2; exit 65; }
# Campaign images set PYTHONPATH during startup. Add the matching Event Service
# channel afterwards, so both the pilot and its payload inherit the extension.
if [[ -n ${EPICPROD_ES_CHANNEL:-} ]]; then
    export PYTHONPATH=$EPICPROD_ES_CHANNEL/python${PYTHONPATH:+:$PYTHONPATH}
fi
# Dispatch uses the catalog spelling, not the resolved filesystem path.
# The server must support container_name before deploying this launcher.
export PILOT_CONTAINER_NAME="$IMAGE"
echo "ePIC native pilot: dispatch container_name=$PILOT_CONTAINER_NAME"
# The proxy HTCondor transferred (x509userproxy): absolute, readable, kept for the payload.
if [[ -n ${X509_USER_PROXY:-} && $X509_USER_PROXY != /* ]]; then
    export X509_USER_PROXY=$HERE/$X509_USER_PROXY
fi
[[ -r ${X509_USER_PROXY:-/nonexistent} ]] || { echo "pilot proxy unreadable: ${X509_USER_PROXY:-unset}" >&2; exit 67; }
export PILOT_X509_USER_PROXY=$X509_USER_PROXY
# The CA bundle and rucio.cfg travel with the pilot; the CVMFS copies are the fallback.
if [[ -f certificates.tar.gz ]]; then tar xzf certificates.tar.gz && export X509_CERT_DIR=$HERE/certificates
else export X509_CERT_DIR=/cvmfs/oasis.opensciencegrid.org/mis/certificates; fi
if [[ -f rucio.cfg ]]; then export RUCIO_CONFIG=$HERE/rucio.cfg
else export RUCIO_CONFIG=/cvmfs/eic.opensciencegrid.org/rucio-clients/rucio.cfg; fi
export RUCIO_ACCOUNT=panda
# The OSG queues advertise root for pilot-log writes; the archive's copy tool reads this.
export EPICPROD_CANARY_LOG_SCHEME=root
python3 - <<'PY'
import json, sys, ssl, psutil, requests, rucio.client, XRootD.client
print(json.dumps({'epic_native_pilot': '20261023', 'python': sys.version.split()[0],
                  'dependencies': 'ssl psutil requests rucio XRootD imported'}))
PY
echo "ePIC native pilot: image $IMAGE, host $(hostname), $(date -u +%FT%TZ), nesting ${EPICPROD_NATIVE_NESTING:-on}"
OSG_APPTAINER=/cvmfs/oasis.opensciencegrid.org/mis/apptainer/1.4.5/bin/apptainer
SESSION_DIR=/var/lib/apptainer/mnt/session
nest() {
    # nest IMAGE CMD...: CMD inside IMAGE through the OSG runtime. When the
    # outer container has no session directory, a private namespace carries
    # one on a tmpfs over /var/lib, as native_container.py does for payloads.
    local image=$1; shift
    if [[ -d $SESSION_DIR ]]; then
        timeout 120 "$OSG_APPTAINER" exec --no-home --bind /cvmfs:/cvmfs "$image" "$@"
    else
        timeout 120 unshare --user --map-current-user --keep-caps --mount --propagation private /bin/bash -c \
            "mount -t tmpfs -o size=1m,nodev,nosuid tmpfs /var/lib && mkdir -p $SESSION_DIR && exec $(printf '%q ' "$OSG_APPTAINER" exec --no-home --bind /cvmfs:/cvmfs "$image" "$@")"
    fi
}
PREFLIGHT=${EPICPROD_NATIVE_PREFLIGHT_IMAGE:-}
if [[ -n $PREFLIGHT ]]; then
    route=$([[ -d $SESSION_DIR ]] && echo "session directory present" || echo "private namespace")
    nest "$PREFLIGHT" /bin/true || {
        rc=$?; set +e; trap - ERR  # the evidence below may itself fail; the exit code must stay 71
        echo "nested preflight in $PREFLIGHT failed (exit $rc, $route); no job taken" >&2
        echo "=== nesting evidence ==="
        env | grep -iE "bindpath|singularity|apptainer" | grep -v "^EPICPROD_" | sort
        echo "--- /var/lib:"; ls -ld /var/lib /var/lib/apptainer 2>&1
        echo "--- unshare: $(unshare --version 2>&1)"; echo "--- mounts:"; grep -E " /cvmfs| /var|overlay" /proc/mounts | cut -c1-160 | head -12
        echo "--- machine ad:"; grep -iE "singularity|apptainer|bindpath|userns|namespace" "${_CONDOR_MACHINE_AD:-/nonexistent}" 2>/dev/null | cut -c1-200 | head -20
        echo "=== end nesting evidence ==="
        exit 71; }
    echo "ePIC native pilot: nested preflight in $PREFLIGHT ok ($route)"
fi
echo "ePIC native pilot: archive $(sha256sum "$ARCHIVE")"
tar xzf "$ARCHIVE"
export PILOT_HOME=$HERE
# A user name for the Rucio client (job-user.sh, transferred with the pilot).
source "$HERE/job-user.sh"
echo "ePIC native pilot: user $USER (uid $(id -u))"
# The glidein's death time, from this execution's machine ad, as the HEPiX
# MachineFeatures shutdowntime the pilot plans against before every further job
# (glidein-deadline.sh, transferred with the pilot). The first job is decided here.
QUEUE=""; prev=""; for a in "$@"; do [[ $prev == -q ]] && QUEUE=$a; prev=$a; done
export PILOT_SITENAME=$QUEUE
source "$HERE/glidein-deadline.sh"
case $DEADLINE_VERDICT in
    ok)    echo "ePIC native pilot: deadline $(date -u -d @"$DEADLINE_AT" +%FT%TZ), $DEADLINE_LEFT s left, minimum for a job $EPICPROD_MIN_TIME_FOR_NEW_JOB s" ;;
    short) echo "glidein life $DEADLINE_LEFT s is under the $EPICPROD_MIN_TIME_FOR_NEW_JOB s a job needs; no job taken" >&2; exit 73 ;;
    *)     echo "ePIC native pilot: no usable deadline in the machine ad or MachineFeatures; the pilot's own clocks apply" ;;
esac
# Pin the pilot's actual clock by execution queue, independently of the
# shared queue's ceiling. Keep every other queue-data field unchanged.
if [[ -r $HERE/runtime-policy.py ]]; then
    source_url="http://pandaserver01.sdcc.bnl.gov:25080/cache/schedconfig/$QUEUE.all.json"
    pilot_args=(); expecting_url=0
    for argument in "$@"; do
        if (( expecting_url )); then source_url=$argument; expecting_url=0
        elif [[ $argument == --queuedata-url ]]; then expecting_url=1
        elif [[ $argument == --queuedata-url=* ]]; then source_url=${argument#*=}
        else pilot_args+=("$argument"); fi
    done
    (( expecting_url == 0 )) || { echo 'missing --queuedata-url value' >&2; exit 66; }
    local_url=$(python3 "$HERE/runtime-policy.py" --policy "${EPICPROD_RUNTIME_POLICY:-$HERE/queue-runtime-policy.json}" \
        --queue "$QUEUE" --machine-ad "${_CONDOR_MACHINE_AD:-}" \
        --prepare-queuedata "$source_url" --output "$HERE/queuedata.json")
    if [[ -n $local_url ]]; then
        set -- "${pilot_args[@]}" --queuedata-url "$local_url"
    fi
fi
# The node guard, before the first job: the pilot's own check covers every
# further one (allow_timefloor). The queue is pilot.py's -q argument.
PILOT_SITENAME=$QUEUE python3 - <<'PY' || { echo "node guard excludes this node; no job taken" >&2; exit 72; }
import json, os, sys, urllib.request
from datetime import datetime, timezone
sys.path.insert(0, 'pilot3')
from pilot.user.epic import landing_check as guard
try:
    with urllib.request.urlopen(guard.EXCLUSION_URL, timeout=guard.EXCLUSION_TIMEOUT_S) as r:
        document = json.loads(r.read().decode())
except Exception as exc:
    print('node guard document not read (%s); proceeding' % exc); sys.exit(0)
entry = guard.excluded_here(document, guard._host_names(), os.environ.get('PILOT_SITENAME', ''),
                            datetime.now(timezone.utc))
if entry is not None:
    print('node guard: this node %s on %s is excluded since %s (%s)'
          % (entry.get('host'), entry.get('queue'), entry.get('since'), entry.get('reason')))
    sys.exit(1)
print('node guard: this node is not excluded (%d listed)' % len(document.get('nodes') or []))
PY
queuedata=()
if [[ -r $HERE/queuedata.json && " $* " != *" --queuedata-url "* ]]; then
    queuedata=(--queuedata-url "file://$HERE/queuedata.json")
fi
exec python3 "$HERE/pilot3/pilot.py" "$@" ${queuedata[@]+"${queuedata[@]}"}
