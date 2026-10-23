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
set -euo pipefail
trap 'echo "ePIC native pilot startup failed at line $LINENO (exit $?)" >&2' ERR
IMAGE=${EPICPROD_NATIVE_IMAGE:?campaign image required}
ARCHIVE=${EPICPROD_NATIVE_ARCHIVE:?transferred pilot archive required}
HERE=$(pwd -P)
ACTUAL=${APPTAINER_CONTAINER:-${SINGULARITY_CONTAINER:-}}
[[ -n $ACTUAL ]] || { echo "not inside a container: the slot ignored +SingularityImage $IMAGE" >&2; exit 69; }
[[ $(readlink -f "$ACTUAL") == $(readlink -f "$IMAGE") ]] || {
    echo "outer image $ACTUAL is not $IMAGE; refusing" >&2; exit 65; }
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
# The glidein's own death time, from this execution's machine ad, as the HEPiX
# MachineFeatures shutdowntime the pilot reads before every further job
# (min of proxy, maxtime and shutdowntime against EPICPROD_MIN_TIME_FOR_NEW_JOB,
# two hours in our archive) and that its monitor honours ten minutes before.
# The first job is the launcher's decision: none with under that much life left.
MIN_LIFE=${EPICPROD_MIN_TIME_FOR_NEW_JOB:-7200}
TODIE=$(grep -m1 "^GLIDEIN_ToDie = " "${_CONDOR_MACHINE_AD:-/nonexistent}" 2>/dev/null | awk "{print \$3}")
if [[ $TODIE =~ ^[0-9]+$ ]]; then
    LEFT=$(( TODIE - $(date +%s) ))
    mkdir -p "$HERE/machinefeatures" && echo "$TODIE" > "$HERE/machinefeatures/shutdowntime" && export MACHINEFEATURES=$HERE/machinefeatures
    echo "ePIC native pilot: glidein dies at $(date -u -d @"$TODIE" +%FT%TZ), $LEFT s from now; MachineFeatures shutdowntime set"
    (( LEFT >= MIN_LIFE )) || { echo "glidein life $LEFT s is under the $MIN_LIFE s a job needs; no job taken" >&2; exit 73; }
else
    echo "ePIC native pilot: no GLIDEIN_ToDie in the machine ad; the pilot's own clocks apply"
fi
# The node guard, before the first job: the pilot's own check covers every
# further one (allow_timefloor). The queue is pilot.py's -q argument.
QUEUE=""; prev=""; for a in "$@"; do [[ $prev == -q ]] && QUEUE=$a; prev=$a; done
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
exec python3 "$HERE/pilot3/pilot.py" "$@"
