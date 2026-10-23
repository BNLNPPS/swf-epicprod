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
    timeout 120 "$OSG_APPTAINER" exec --no-home --bind /cvmfs:/cvmfs "$PREFLIGHT" /bin/true || {
        echo "nested preflight in $PREFLIGHT failed (exit $?); no job taken" >&2; exit 71; }
    echo "ePIC native pilot: nested preflight in $PREFLIGHT ok"
fi
[[ ! -e pilot3 ]] || { echo 'pilot3 already present in the sandbox' >&2; exit 66; }
echo "ePIC native pilot: archive $(sha256sum "$ARCHIVE")"
tar xzf "$ARCHIVE"
export PILOT_HOME=$HERE
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
