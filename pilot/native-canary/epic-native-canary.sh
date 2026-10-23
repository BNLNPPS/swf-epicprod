#!/bin/bash
# Isolated canary only. Does not touch any production wrapper or pointer.
set -euo pipefail
trap 'echo "ePIC canary startup failed at line $LINENO (exit $?)" >&2' ERR
IMAGE=${EPICPROD_CANARY_IMAGE:?exact campaign image required}
TASK=${EPICPROD_CANARY_TASK_ID:?explicit test task required}
QUEUE=${EPICPROD_CANARY_QUEUE:?queue required}
ARCHIVE=${EPICPROD_CANARY_ARCHIVE:?transferred pilot archive required}
HERE=$(pwd -P)
SELF=$(readlink -f "$0")
ACTUAL=${APPTAINER_CONTAINER:-${SINGULARITY_CONTAINER:-}}

if [[ ${1:-} != --environment-only ]]; then
    # The CE's batch adapter can replace X509_USER_PROXY with a path in
    # its own spool, outside the job sandbox. Use our explicitly shipped
    # canary credential inside the directory we bind into the image.
    export X509_USER_PROXY=$HERE/pilot-proxy
    export PILOT_X509_USER_PROXY=$X509_USER_PROXY
    [[ -r $X509_USER_PROXY ]] || { echo 'Transferred canary proxy is unreadable' >&2; exit 67; }
fi

if [[ -z $ACTUAL ]]; then
    RUNTIME=$(command -v apptainer || command -v singularity || true)
    if [[ -z $RUNTIME ]]; then
        RUNTIME=/cvmfs/oasis.opensciencegrid.org/mis/apptainer/1.4.5/$(uname -m)/bin/apptainer
    fi
    [[ -x $RUNTIME ]] || { echo "Apptainer unavailable: $RUNTIME" >&2; exit 69; }
    echo "ePIC canary: native batch host $(hostname), runtime $RUNTIME"
    exec "$RUNTIME" exec --no-home --bind /cvmfs:/cvmfs --bind "$HERE:$HERE" --bind "$SELF:$SELF" \
        --pwd "$HERE" "$IMAGE" /bin/bash "$SELF" "$@"
fi
[[ $(readlink -f "$ACTUAL") == $(readlink -f "$IMAGE") ]] || {
    echo 'Wrong outer image; refusing nested launch' >&2; exit 65;
}
export X509_CERT_DIR=/cvmfs/oasis.opensciencegrid.org/mis/certificates
export RUCIO_CONFIG=/cvmfs/eic.opensciencegrid.org/rucio-clients/rucio.cfg
export RUCIO_ACCOUNT=panda
# Both test queues advertise root for pilot-log writes. The image's Rucio
# client otherwise selected davs, which uploaded but failed PanDA registration.
export EPICPROD_CANARY_LOG_SCHEME=root
# Credentials stay in the transferred directory. Do not copy their contents
# into logs, a private home, or an individual bind mount.
for key in X509_USER_PROXY PANDA_AUTH_TOKEN PANDA_AUTH_TOKEN_KEY OIDC_AUTH_TOKEN; do
    value=${!key:-}
    if [[ -n $value && $value != /* ]]; then
        printf -v "$key" '%s/%s' "$HERE" "$value"
        export "$key"
    fi
done
python3 - <<'PY'
import json, sys, ssl, psutil, requests, rucio.client, XRootD.client
assert sys.version_info >= (3, 9)
print(json.dumps({'epic_native_canary': '20261022', 'python': sys.version,
                  'dependencies': 'ssl psutil requests rucio XRootD imported'}))
PY
if [[ ${1:-} == --environment-only ]]; then exit 0; fi
[[ ! -e pilot3 ]] || { echo 'Refusing to overwrite an existing pilot tree' >&2; exit 66; }
tar xzf "$ARCHIVE"
export PILOT_HOME=$HERE
export LOCAL_AGIS_SCHEDCONF=$HERE/agis_schedconf.json
exec python3 "$HERE/pilot3/pilot.py" \
    -q "$QUEUE" -i PR -j test -w generic --pilot-user epic \
    --url https://pandaserver01.sdcc.bnl.gov -p 25443 \
    --rucio-host https://nprucio01.sdcc.bnl.gov:443 \
    --queuedata-url "file://$HERE/queuedata.json" \
    --storagedata-url "file://$HERE/ddmendpoints.json" \
    --allow-same-user=False --job-type=managed --resource-type SCORE \
    --getjobrequests=3 --cleanup=False --noworkerpilotstatusupdate --debug
