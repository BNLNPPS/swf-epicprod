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
if [ "${EPICPROD_TEST_JLAB_UNREACHABLE:-0}" = "1" ] && [ -n "${RUCIO_CONFIG:-}" ] \
   && [ -f "${RUCIO_CONFIG}" ]; then
  _test_cfg="${TMPDIR:-/tmp}/rucio-jlab-unreachable.cfg"
  sed -E 's#^(rucio_host|auth_host)[[:space:]]*=.*#\1 = https://127.0.0.1:9#' \
    "${RUCIO_CONFIG}" > "${_test_cfg}"
  export RUCIO_CONFIG="${_test_cfg}"
  echo "TEST: JLab catalog unreachable for registration (RUCIO_CONFIG=${RUCIO_CONFIG})"
fi
