#!/bin/bash
# Build and install Simphony from the synrad branch, then build the synrad
# example (GPU soft X-ray SR transport + Geant4 reference mode) against the
# install (VOLUNTEER_GPU_PLAN.md, synchrotron-radiation track). Works on a
# GPU-less host: compilation needs no device.
#
# Usage: build-simphony-synrad.sh <simphony-src-dir> <build-dir> <install-prefix>
#
# The synrad example is an external project (find_package(simphony)), so a
# full install is required, unlike the core-target build of
# build-simphony-core.sh. Recipe notes:
# - gcc, not the container's default clang (plog init dies in a clang build);
#   OptiX include dir passed explicitly. Both inherited from the core recipe.
# - BUILD_TESTING=OFF keeps known-broken test units out of the `all` target
#   the install depends on. Test executables (e.g. CSGOptiXServiceTest for
#   the containment work) are built separately with a reconfigure.
# - The container carries a spack-installed simphony; our install prefix goes
#   FIRST in CMAKE_PREFIX_PATH so find_package resolves the branch build.
# - CMAKE_BUILD_TYPE=Release matches the upstream release image (Dockerfile)
#   and the examples/synrad speedup.sh methodology, so performance numbers
#   are comparable with the published ones. An unoptimized build passes the
#   same physics validation but distorts timing on both sides.
# - The example CMake pulls glm before simphony (link-interface ordering);
#   both resolve from the container's spack environment. The synrad_g4
#   target links no simphony libraries (header-only use) so it never
#   inherits usage requirements from a link interface, and sphoton.h pulls
#   a header chain needing glm (stra.h uses gtx extensions, gated behind
#   GLM_ENABLE_EXPERIMENTAL) and plog (OpticksPhoton.hh). On a default
#   path in the upstream CI image, in spack prefixes here — all passed
#   explicitly via CMAKE_CXX_FLAGS.

set -euo pipefail

SRC=${1:?simphony source dir}
BUILD=${2:?build dir}
PREFIX=${3:?install prefix}
CONTAINER=/cvmfs/singularity.opensciencegrid.org/eicweb/eic_dev_cuda:nightly

BINDS=()
for d in "$(dirname "$SRC")" "$(dirname "$BUILD")" "$(dirname "$PREFIX")"; do
    case "$d" in
        "$HOME"*|/tmp*) ;;                    # default binds
        *) BINDS+=(--bind "$d") ;;
    esac
done

apptainer exec "${BINDS[@]}" "$CONTAINER" bash -c "
set -euo pipefail
export CC=/usr/bin/gcc CXX=/usr/bin/g++
OPTIX_INC=\$(ls -d /opt/software/linux-x86_64_v2/optix-dev-*/include | head -1)
cmake -S '$SRC' -B '$BUILD' \
    -DOptiX_INCLUDE_DIR=\$OPTIX_INC \
    -DCMAKE_CUDA_HOST_COMPILER=/usr/bin/g++ \
    -DBUILD_TESTING=OFF \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX='$PREFIX'
cmake --build '$BUILD' -j12
cmake --install '$BUILD'
GLM_INC=\$(ls -d /opt/software/linux-x86_64_v2/glm-*/include | head -1)
PLOG_INC=\$(ls -d /opt/software/linux-x86_64_v2/plog-*/include | head -1)
cmake -S '$SRC/examples/synrad' -B '$BUILD-example' \
    -DCMAKE_PREFIX_PATH='$PREFIX' \
    -DCMAKE_INSTALL_PREFIX='$PREFIX' \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_CXX_FLAGS=\"-I\$GLM_INC -I\$PLOG_INC -DGLM_ENABLE_EXPERIMENTAL\"
cmake --build '$BUILD-example' -j4
cmake --install '$BUILD-example'
"
echo "installed: $PREFIX/bin/synrad $PREFIX/bin/synrad_g4"
