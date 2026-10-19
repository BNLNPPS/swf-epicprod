# TEST ONLY. Sourced by run.sh at its registration step and by
# es/es_close.sh after it sets RUCIO_CONFIG: with
# EPICPROD_TEST_JLAB_UNREACHABLE=1 in the job environment (a canary's
# --canary-jlab-unreachable, never a production configuration), the JLab
# catalog is made unreachable to this job's registration by pointing
# RUCIO_CONFIG at a copy of the payload's rucio.cfg whose server is a
# refused local port. The landing check and the delivered-output check have
# already run against the real server, so the job does its work and meets a
# dead catalog only at registration: the failover paths of
# RUCIO_FAILOVER_STASH.md (preserved and pending at BNL-XRD, or stashed
# there from another output RSE) run as they would in a JLab outage.
# The copy is made from, and replaces, the X.509 config rucio_auth builds
# on (rucio_auth.sh), so the token config it writes later points at the
# refused port too.
_test_src="${RUCIO_X509_CONFIG:-${RUCIO_CONFIG:-}}"
if [ "${EPICPROD_TEST_JLAB_UNREACHABLE:-0}" = "1" ] && [ -n "${_test_src}" ] \
   && [ -f "${_test_src}" ]; then
  _test_cfg="${TMPDIR:-/tmp}/rucio-jlab-unreachable.cfg"
  sed -E 's#^(rucio_host|auth_host)[[:space:]]*=.*#\1 = https://127.0.0.1:9#' \
    "${_test_src}" > "${_test_cfg}"
  export RUCIO_CONFIG="${_test_cfg}"
  RUCIO_X509_CONFIG="${_test_cfg}"
  echo "TEST: JLab catalog unreachable for registration (RUCIO_CONFIG=${RUCIO_CONFIG})"
fi
