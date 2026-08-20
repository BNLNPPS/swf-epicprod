#!/bin/bash
# Run the synrad GPU-vs-Geant4 validation on this host (VOLUNTEER_GPU_PLAN.md,
# synchrotron-radiation track): transport the same pencil-beam photons through
# the SynradBenchmark tunnel with the GPU reflect-or-absorb mode and the
# Geant4 reference app, then compare the wall-absorption records
# statistically (optiphy/ana/synrad_test.py). Requires a GPU (--nv).
#
# Usage: run-synrad-trial.sh <simphony-src-dir> <install-prefix> <trial-dir> [nphoton] [seed]
#
# Mirrors tests/test_synrad_example.sh (same beam, LOST-0 gate, same
# comparison) but uses the binaries installed by build-simphony-synrad.sh
# instead of rebuilding, takes the GDML from the source tree (the install
# carries only the executables), and pins device 0 — device 1 is left for
# the PanDA pilot, per NPPS0_WORKER.md.

set -euo pipefail

SRC=${1:?simphony source dir}
PREFIX=${2:?simphony install prefix}
TRIAL=${3:?trial dir}
NPHOTON=${4:-500000}
SEED=${5:-42}
BEAM="0,0,100,0,0.007,1,0.3,19.4"
CONTAINER=/cvmfs/singularity.opensciencegrid.org/eicweb/eic_dev_cuda:nightly

BINDS=()
for d in "$(dirname "$SRC")" "$(dirname "$PREFIX")" "$(dirname "$TRIAL")"; do
    case "$d" in
        "$HOME"*|/tmp*) ;;                    # default binds
        *) BINDS+=(--bind "$d") ;;
    esac
done

mkdir -p "$TRIAL"

apptainer exec --nv "${BINDS[@]}" "$CONTAINER" bash -c "
set -euo pipefail
export LD_LIBRARY_PATH='$PREFIX/lib:$PREFIX/lib64'\${LD_LIBRARY_PATH:+:\$LD_LIBRARY_PATH}
export OPTICKS_MAX_SLOT=\$(( $NPHOTON + 100000 ))
export CUDA_VISIBLE_DEVICES=0

echo '== GPU: synrad (coarse fused envelope) =='
'$PREFIX/bin/synrad' -g '$SRC/examples/synrad/synrad_bench.gdml' \
    -n $NPHOTON -s $SEED -I $BEAM -f 0 -o '$TRIAL' 2>&1 | grep '^synrad:'

echo '== G4 reference: synrad_g4 (analytic CSG) =='
'$PREFIX/bin/synrad_g4' -g analytic -n $NPHOTON -s $SEED -I $BEAM -f 0 \
    -o '$TRIAL' 2>&1 | grep '^synrad-g4' | tee '$TRIAL/g4_summary.txt'

grep -q 'LOST 0 ' '$TRIAL/g4_summary.txt' \
    || { echo 'FAIL: G4 mode lost tracks (LOST != 0)' >&2; exit 1; }

echo '== compare wall-absorption records =='
python3 '$SRC/optiphy/ana/synrad_test.py' \
    '$TRIAL/synrad_hits.npy' '$TRIAL/synrad_g4_hits.npy' --nphoton $NPHOTON
"
