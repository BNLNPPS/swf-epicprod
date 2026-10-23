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
# Nested launches run the OSG pool's Apptainer on CVMFS through its wrapper and
# need /var/lib/apptainer/mnt/session in the outer container, which the submit
# description binds in from CVMFS (+SingularityBind). Without it no job is taken.
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
export X509_CERT_DIR=/cvmfs/oasis.opensciencegrid.org/mis/certificates
export RUCIO_CONFIG=/cvmfs/eic.opensciencegrid.org/rucio-clients/rucio.cfg
export RUCIO_ACCOUNT=panda
# The OSG queues advertise root for pilot-log writes; the archive's copy tool reads this.
export EPICPROD_CANARY_LOG_SCHEME=root
python3 - <<'PY'
import json, sys, ssl, psutil, requests, rucio.client, XRootD.client
print(json.dumps({'epic_native_pilot': '20261023', 'python': sys.version.split()[0],
                  'dependencies': 'ssl psutil requests rucio XRootD imported'}))
PY
echo "ePIC native pilot: image $IMAGE, host $(hostname), $(date -u +%FT%TZ)"
OSG_APPTAINER=/cvmfs/oasis.opensciencegrid.org/mis/apptainer/1.4.5/bin/apptainer
[[ -d /var/lib/apptainer/mnt/session ]] || {
    echo "no /var/lib/apptainer/mnt/session in the outer container: the +SingularityBind was not honored here; no job taken" >&2; exit 70; }
PREFLIGHT=${EPICPROD_NATIVE_PREFLIGHT_IMAGE:-}
if [[ -n $PREFLIGHT ]]; then
    "$OSG_APPTAINER" exec --no-home --bind /cvmfs:/cvmfs "$PREFLIGHT" /bin/true || {
        echo "nested preflight in $PREFLIGHT failed (exit $?); no job taken" >&2; exit 71; }
    echo "ePIC native pilot: nested preflight in $PREFLIGHT ok"
fi
[[ ! -e pilot3 ]] || { echo 'pilot3 already present in the sandbox' >&2; exit 66; }
tar xzf "$ARCHIVE"
export PILOT_HOME=$HERE
exec python3 "$HERE/pilot3/pilot.py" "$@"
