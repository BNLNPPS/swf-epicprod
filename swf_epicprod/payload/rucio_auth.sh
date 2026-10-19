# Sourced by run.sh and es/es_close.sh after RUCIO_CONFIG is set: Rucio with a
# token from PanDA when the job has a PanDA token, else the X.509 proxy.
RUCIO_AUTH_PAYLOAD=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
RUCIO_X509_CONFIG=${RUCIO_X509_CONFIG:-${RUCIO_CONFIG}}
RUCIO_AUTH_DIR=${RUCIO_AUTH_DIR:-${TMPDIR:-${PWD}}}
rucio_auth() {
  local dir=${TMPDIR:-${RUCIO_AUTH_DIR}}
  if python "${RUCIO_AUTH_PAYLOAD}/tokens.py" rucio \
      --out "${dir}/rucio_token" --cfg "${dir}/rucio-token.cfg" --base-cfg "${RUCIO_X509_CONFIG}"; then
    export RUCIO_CONFIG="${dir}/rucio-token.cfg"
    unset BEARER_TOKEN BEARER_TOKEN_FILE
    echo "rucio auth: token from PanDA"
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
