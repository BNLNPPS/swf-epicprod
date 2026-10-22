#!/bin/bash
# JLab: CE authentication uses SciTokens; the pilot credential is an ordinary
# protected input. Resolve it before the standard wrapper changes directory.
set -eu
inputs=$PWD
export X509_USER_PROXY="$inputs/x509up_u1000"
test -r "$X509_USER_PROXY" || { echo 'JLab pilot: transferred proxy is missing' >&2; exit 65; }
export ATLAS_LOCAL_PYTHON=true
export ALRB_noGridMW=YES
export X509_CERT_DIR=/cvmfs/oasis.opensciencegrid.org/mis/certificates
echo 'JLab pilot: native Python, transferred pilot proxy; no ATLAS Python setup'
exec bash "$inputs/bnlpanda.runpilot2-wrapper.sh" "$@" --localpy
