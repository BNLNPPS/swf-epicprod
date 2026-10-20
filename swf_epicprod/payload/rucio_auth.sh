# Sourced by run.sh and es/es_close.sh after RUCIO_CONFIG is set: Rucio with a
# token from PanDA when the job has a PanDA token, else the X.509 proxy.
RUCIO_AUTH_PAYLOAD=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
RUCIO_X509_CONFIG=${RUCIO_X509_CONFIG:-${RUCIO_CONFIG}}
RUCIO_AUTH_DIR=${RUCIO_AUTH_DIR:-${TMPDIR:-${PWD}}}
RUCIO_PATCHED_LIB=
# The payload's patched Rucio client (tokens.py patched-client) leads
# PYTHONPATH only for a step that uses a token; an X.509 step runs the
# image's own client, as before.
rucio_patched_client_off() {
  [ -n "${RUCIO_PATCHED_LIB}" ] || return 0
  local p=":${PYTHONPATH:-}:"
  p=${p//":${RUCIO_PATCHED_LIB}:"/:}
  p=${p#:}; p=${p%:}
  if [ -n "${p}" ]; then export PYTHONPATH="${p}"; else unset PYTHONPATH; fi
  RUCIO_PATCHED_LIB=
}
rucio_auth() {
  # run.sh's ERR trap ends the job on a failing command outside a test, so
  # every command here that may fail is one.
  local dir=${TMPDIR:-${RUCIO_AUTH_DIR}} lib rc client="the image's Rucio client"
  rucio_patched_client_off
  if python "${RUCIO_AUTH_PAYLOAD}/tokens.py" rucio \
      --out "${dir}/rucio_token" --cfg "${dir}/rucio-token.cfg" --base-cfg "${RUCIO_X509_CONFIG}"; then
    # The token upload needs a client with the token upload patch: the
    # image's own, or the payload's where the image has 40.2.0 without it.
    if lib=$(python "${RUCIO_AUTH_PAYLOAD}/tokens.py" patched-client --dir "${dir}/rucio-clients"); then
      rc=0
    else
      rc=$?
    fi
    if [ ${rc} -ne 0 ] && [ ${rc} -ne 4 ]; then
      export RUCIO_CONFIG="${RUCIO_X509_CONFIG}"
      echo "rucio auth: X.509 proxy (no Rucio client here uploads with a token)"
      return 0
    fi
    if [ ${rc} -eq 0 ]; then
      RUCIO_PATCHED_LIB=${lib}
      export PYTHONPATH="${lib}${PYTHONPATH:+:${PYTHONPATH}}"
      client="the payload's patched Rucio client"
    fi
    export RUCIO_CONFIG="${dir}/rucio-token.cfg"
    unset BEARER_TOKEN BEARER_TOKEN_FILE
    echo "rucio auth: token from PanDA, ${client}"
  else
    export RUCIO_CONFIG="${RUCIO_X509_CONFIG}"
    echo "rucio auth: X.509 proxy"
  fi
}
stash_token_auth() {
  # $1 door, $2 path at the door; sets XRD_AUTH (env prefix), empty for X.509.
  # create and modify, so the stash overwrites as xrdcp -f does with X.509.
  local tok
  XRD_AUTH=()
  if tok=$(python "${RUCIO_AUTH_PAYLOAD}/tokens.py" storage \
      --door "$1" --path "$2" --base-path "${STASH_TOKEN_BASE:-}" \
      --verb storage.create --verb storage.modify); then
    XRD_AUTH=(env XrdSecPROTOCOL=ztn "BEARER_TOKEN=${tok}")
  fi
}
