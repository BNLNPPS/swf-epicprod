#!/bin/bash
# Build the Simphony core and the CSGOptiXService test executable in the
# eic_dev_cuda container (VOLUNTEER_GPU_PLAN.md, the Linux containment
# trial). Works on a GPU-less host: compilation needs no device.
#
# Usage: build-simphony-core.sh <simphony-src-dir> <build-dir>
#
# Recipe notes, each paid for during bring-up on pandaserver02:
# - The container defaults CC/CXX to clang; the spack-built Simphony the
#   payload runs was built with g++, and a clang build dies at startup in
#   plog logging init (PLOG_LOCAL visibility). Build with gcc explicitly.
# - The OptiX SDK lives in the container's spack tree; FindOptiX needs
#   the include dir passed.
# - The full default build includes a test unit that does not compile on
#   current main; building the CSGOptiXServiceTest target avoids it and
#   pulls in the whole core chain (SysRap, CSG, QUDARap, CSGOptiX).
# - /data is not bound by default; bind it when trees live there.

set -euo pipefail

SRC=${1:?simphony source dir}
BUILD=${2:?build dir}
CONTAINER=/cvmfs/singularity.opensciencegrid.org/eicweb/eic_dev_cuda:nightly

apptainer exec --bind /data "$CONTAINER" bash -c "
set -euo pipefail
export CC=/usr/bin/gcc CXX=/usr/bin/g++
OPTIX_INC=\$(ls -d /opt/software/linux-x86_64_v2/optix-dev-*/include | head -1)
cmake -S '$SRC' -B '$BUILD' \
    -DOptiX_INCLUDE_DIR=\$OPTIX_INC \
    -DCMAKE_CUDA_HOST_COMPILER=/usr/bin/g++
cmake --build '$BUILD' --target CSGOptiXServiceTest -j8
"
echo "built: $BUILD/CSGOptiX/tests/CSGOptiXServiceTest"
