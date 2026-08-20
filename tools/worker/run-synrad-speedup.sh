#!/bin/bash
# Run the upstream synrad speedup measurement (simphony
# examples/synrad/speedup.sh) on this host, unmodified, against the Release
# install from build-simphony-synrad.sh — so the four timings (Geant4
# analytic, Geant4 tessellated Delta, GPU coarse, GPU fine) and the two
# ratios are directly comparable with the published branch numbers.
# Requires a GPU (--nv); device 0 pinned, device 1 left for the pilot.
#
# Usage: run-synrad-speedup.sh <simphony-src-dir> <install-prefix> <run-base-dir> [nphoton] [seed]
#
# Recipe notes:
# - The upstream script is invoked verbatim; the wrapper supplies through
#   the environment what the upstream CI image has on default paths: gcc
#   (the container defaults to clang, which dies at startup in plog init)
#   and the glm/plog spack includes plus GLM_ENABLE_EXPERIMENTAL via
#   CXXFLAGS, honored by CMake at the script's first configure of its
#   example build.
# - The script's example build dir is removed first so the configure is
#   fresh and CXXFLAGS take effect.
# - The Geant4 tessellated Delta leg tracks the SR electrons through the
#   meshed geometry twice; minutes of single-core CPU at the default
#   500k-photon scale is expected.

set -euo pipefail

SRC=${1:?simphony source dir}
PREFIX=${2:?simphony install prefix}
RUNBASE=${3:?run base dir}
NPHOTON=${4:-500000}
SEED=${5:-42}
CONTAINER=/cvmfs/singularity.opensciencegrid.org/eicweb/eic_dev_cuda:nightly

BINDS=()
for d in "$(dirname "$SRC")" "$(dirname "$PREFIX")" "$(dirname "$RUNBASE")"; do
    case "$d" in
        "$HOME"*|/tmp*) ;;                    # default binds
        *) BINDS+=(--bind "$d") ;;
    esac
done

rm -rf "$RUNBASE/build"
mkdir -p "$RUNBASE"

apptainer exec --nv "${BINDS[@]}" "$CONTAINER" bash -c "
set -euo pipefail
export CC=/usr/bin/gcc CXX=/usr/bin/g++
GLM_INC=\$(ls -d /opt/software/linux-x86_64_v2/glm-*/include | head -1)
PLOG_INC=\$(ls -d /opt/software/linux-x86_64_v2/plog-*/include | head -1)
export CXXFLAGS=\"-I\$GLM_INC -I\$PLOG_INC -DGLM_ENABLE_EXPERIMENTAL\"
export SIMPHONY_PREFIX='$PREFIX'
export SIMPHONY_LIB_DIR='$PREFIX/lib:$PREFIX/lib64'
export SIMPHONY_SYNRAD_BUILD_DIR='$RUNBASE/build'
export SIMPHONY_SYNRAD_RUN_DIR='$RUNBASE/run'
export CUDA_VISIBLE_DEVICES=0
cd '$RUNBASE'    # the apps write OPTICKS_LOG and run-metadata files to the CWD
'$SRC/examples/synrad/speedup.sh' $NPHOTON $SEED
"
