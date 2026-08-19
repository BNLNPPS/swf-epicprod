#!/bin/bash
# Capture the containment-trial inputs (VOLUNTEER_GPU_PLAN.md, the Linux
# containment trial): a persisted CSGFoundry geometry bundle and genstep
# arrays, produced by running the raindrop DD4hep GPU example with geometry
# saving and event-array output enabled. Requires a GPU (--nv).
#
# Usage: capture-simphony-trial-inputs.sh <simphony-src-dir> <trial-dir>
#
# Recipe notes:
# - The example hardcodes SaveGeometry=False; a scratch copy under the
#   trial dir is patched, leaving the simphony clone pristine.
# - G4CXOpticks::SaveGeometry() writes only when the
#   G4CXOpticks__SaveGeometry_DIR envvar is set (g4cx/G4CXOpticks.cc);
#   the saved directory is the CFBASE for CSGFoundry::Load().
# - OPTICKS_EVENT_MODE=Hit persists hit.npy + genstep.npy per event
#   (simphony docs/inputs-outputs.md); OPTICKS_OUT_FOLD is the output base.

set -euo pipefail

SRC=${1:?simphony source dir}
TRIAL=${2:?trial dir}
CONTAINER=/cvmfs/singularity.opensciencegrid.org/eicweb/eic_dev_cuda:nightly

SCRATCH=$TRIAL/example
mkdir -p "$SCRATCH" "$TRIAL/geom" "$TRIAL/events"
cp "$SRC/dd4hepplugins/examples/test_raindrop_dd4hep_gpu.py" "$SCRATCH/"
cp -r "$SRC/dd4hepplugins/examples/geometry" "$SCRATCH/"
sed -i 's/SaveGeometry = False/SaveGeometry = True/' "$SCRATCH/test_raindrop_dd4hep_gpu.py"
grep -q "SaveGeometry = True" "$SCRATCH/test_raindrop_dd4hep_gpu.py"

BIND=()
[ -d /data ] && BIND+=(--bind /data)
apptainer exec --nv "${BIND[@]}" "$CONTAINER" bash -c "
set -euo pipefail
export G4CXOpticks__SaveGeometry_DIR='$TRIAL/geom'
export OPTICKS_EVENT_MODE=Hit
export OPTICKS_OUT_FOLD='$TRIAL/events'
cd '$SCRATCH'
python3 test_raindrop_dd4hep_gpu.py
"

echo "== geometry bundle =="
find "$TRIAL/geom" -maxdepth 2 | head -20
echo "== genstep arrays =="
find "$TRIAL/events" -name 'genstep.npy'
