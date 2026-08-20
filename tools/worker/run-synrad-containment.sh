#!/bin/bash
# Run the synchrotron-radiation containment proof (VOLUNTEER_GPU_PLAN.md,
# synchrotron-radiation track): synrad_service transports the validation
# pencil-beam photons through the persisted CSGFoundry chamber geometry with
# no Geant4 in the process, and the wall-absorption records are compared
# statistically against the Geant4 reference hits from run-synrad-trial.sh.
# Requires a GPU (--nv); device 0 pinned, device 1 left for the pilot.
#
# Usage: run-synrad-containment.sh <simphony-src-dir> <install-prefix> <trial-dir> [nphoton] [seed]
#
# Recipe notes:
# - Geometry capture: a short synrad run with
#   G4CXOpticks__setGeometry_saveGeometry=<dir> persists the CSGFoundry
#   translation of the tessellated chamber automatically at SetGeometry
#   time (g4cx/G4CXOpticks.cc). The G4CXOpticks__SaveGeometry_DIR mechanism
#   the raindrop capture used additionally requires an explicit
#   G4CXOpticks::SaveGeometry() call, which the synrad app does not make.
#   The capture is skipped when the bundle already exists. This is the one
#   Geant4-using step, and it happens in a separate process from the replay.
# - Geometry resolution in the replay is two envvars (spath::CFBaseFromGEOM):
#   GEOM names the geometry and <GEOM>_CFBaseFromGEOM points at the
#   directory CONTAINING CSGFoundry/.
# - The ldd gate proves the containment claim before the replay runs:
#   no Geant4 and no G4CX in the process image.
# - The comparison target is the synrad_g4 reference record produced by
#   run-synrad-trial.sh in the same trial dir, so the trial must run first.

set -euo pipefail

SRC=${1:?simphony source dir}
PREFIX=${2:?simphony install prefix}
TRIAL=${3:?trial dir}
NPHOTON=${4:-500000}
SEED=${5:-42}
BEAM="0,0,100,0,0.007,1,0.3,19.4"
CONTAINER=/cvmfs/singularity.opensciencegrid.org/eicweb/eic_dev_cuda:nightly

G4HITS=$TRIAL/synrad_g4_hits.npy
[ -f "$G4HITS" ] || { echo "missing G4 reference hits: $G4HITS (run run-synrad-trial.sh first)" >&2; exit 1; }

BINDS=()
for d in "$(dirname "$SRC")" "$(dirname "$PREFIX")" "$(dirname "$TRIAL")"; do
    case "$d" in
        "$HOME"*|/tmp*) ;;                    # default binds
        *) BINDS+=(--bind "$d") ;;
    esac
done

mkdir -p "$TRIAL/geom"

apptainer exec --nv "${BINDS[@]}" "$CONTAINER" bash -c "
set -euo pipefail
export LD_LIBRARY_PATH='$PREFIX/lib:$PREFIX/lib64'\${LD_LIBRARY_PATH:+:\$LD_LIBRARY_PATH}
export CUDA_VISIBLE_DEVICES=0
cd '$TRIAL'    # the apps write OPTICKS_LOG and run-metadata files to the CWD

if [ ! -d '$TRIAL/geom/CSGFoundry' ]; then
    echo '== capture: persist CSGFoundry from a short synrad run =='
    export G4CXOpticks__setGeometry_saveGeometry='$TRIAL/geom'
    '$PREFIX/bin/synrad' -g '$SRC/examples/synrad/synrad_bench.gdml' \
        -n 1000 -s $SEED -o '$TRIAL/geom' 2>&1 | grep '^synrad:'
    unset G4CXOpticks__setGeometry_saveGeometry
    [ -d '$TRIAL/geom/CSGFoundry' ] || { echo 'FAIL: no CSGFoundry persisted' >&2; exit 1; }
else
    echo '== capture: reusing existing CSGFoundry bundle =='
fi

echo '== containment check: Geant4/G4CX libs in the process image =='
if ldd '$PREFIX/bin/synrad_service' | grep -iE 'geant4|G4CX'; then
    echo 'FAIL: Geant4/G4CX linked' >&2; exit 1
fi
echo 'none linked'

echo '== replay: synrad_service on persisted geometry, no Geant4 =='
export GEOM=synrad
export synrad_CFBaseFromGEOM='$TRIAL/geom'
export OPTICKS_MAX_SLOT=\$(( $NPHOTON + 100000 ))
'$PREFIX/bin/synrad_service' -n $NPHOTON -s $SEED -I $BEAM -f 0 -o '$TRIAL' \
    2>&1 | grep '^synrad-service:'

echo '== compare containment hits vs the G4 reference record =='
python3 '$SRC/optiphy/ana/synrad_test.py' \
    '$TRIAL/synrad_service_hits.npy' '$G4HITS' --nphoton $NPHOTON
"
