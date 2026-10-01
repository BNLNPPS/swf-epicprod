# Sourced by run.sh and es/es_close.sh inside the task's image: the grid
# CA directory every xrootd and Rucio client of the payload uses.
#
# The image sets X509_CERT_DIR to its own copy (/opt/local/certificates:
# the OSG CA set as of the image build, no revocation lists), and its
# environment overrides one passed in at container start. From a GKE pod
# that copy failed both BNL doors (epicxrd1 "TLS error", dcintdoor "Auth
# failed"), so the stash failover had nowhere to write; the current OSG set
# on CVMFS passed both (2026-10-01). Where CVMFS carries it, it is used.
OSG_CERT_DIR=/cvmfs/oasis.opensciencegrid.org/mis/certificates
if [ -d "${OSG_CERT_DIR}" ]; then
  export X509_CERT_DIR=${OSG_CERT_DIR}
fi
echo "grid CA directory: ${X509_CERT_DIR:-unset}"
