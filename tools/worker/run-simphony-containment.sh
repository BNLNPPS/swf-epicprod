#!/bin/bash
# Run the containment proof (VOLUNTEER_GPU_PLAN.md, the Linux containment
# trial): CSGOptiXServiceTest standalone against the captured CSGFoundry
# geometry and genstep arrays — gensteps in, hits out, no Geant4 in the
# process. Requires a GPU (--nv).
#
# Usage: run-simphony-containment.sh <build-dir> <trial-dir>
#
# Recipe notes:
# - Geometry resolution is two envvars (spath::CFBaseFromGEOM, not a bare
#   CFBASE): GEOM names the geometry and <GEOM>_CFBaseFromGEOM points at
#   the directory CONTAINING CSGFoundry/.
# - OPTICKS_INPUT_GENSTEP feeds SEvt::createInputGenstep_configured; no
#   special running mode is needed (sysrap/SEvt.cc,
#   createInputGenstep_simulate: any configured input-genstep path is used).
# - OPTICKS_EVENT_MODE=Hit + OPTICKS_OUT_FOLD persist the replay's
#   hit.npy and genstep.npy for the reference set.
# - CUDA_VISIBLE_DEVICES pins one device; the service warns on multi-GPU
#   hosts and defaults to device 0 anyway.

set -euo pipefail

BUILD=${1:?simphony build dir}
TRIAL=${2:?trial dir}
CONTAINER=/cvmfs/singularity.opensciencegrid.org/eicweb/eic_dev_cuda:nightly

EXE=$BUILD/CSGOptiX/tests/CSGOptiXServiceTest
GS=$TRIAL/events/ALL0_no_opticks_event_name/A000/genstep.npy
[ -f "$GS" ] || { echo "missing genstep: $GS" >&2; exit 1; }
LDP=$(find "$BUILD" -name "*.so*" -printf "%h\n" | sort -u | paste -sd:)

BIND=()
[ -d /data ] && BIND+=(--bind /data)
apptainer exec --nv "${BIND[@]}" "$CONTAINER" bash -c "
set -euo pipefail
export LD_LIBRARY_PATH=$LDP:\$LD_LIBRARY_PATH
echo '== containment check: Geant4 libs in the process image =='
if ldd '$EXE' | grep -i geant4; then echo 'FAIL: Geant4 linked' >&2; exit 1; fi
echo 'none linked'
export GEOM=trial
export trial_CFBaseFromGEOM='$TRIAL/geom'
export OPTICKS_INPUT_GENSTEP='$GS'
export OPTICKS_EVENT_MODE=Hit
export OPTICKS_OUT_FOLD='$TRIAL/replay'
export CUDA_VISIBLE_DEVICES=0
'$EXE'
"

echo "== replay outputs =="
find "$TRIAL/replay" -type f 2>/dev/null | head -20
