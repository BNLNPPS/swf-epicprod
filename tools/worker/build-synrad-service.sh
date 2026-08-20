#!/bin/bash
# Build the synrad_service containment executable (synrad-service/) against
# the Simphony install produced by build-simphony-synrad.sh
# (VOLUNTEER_GPU_PLAN.md, synchrotron-radiation track). Works on a GPU-less
# host: compilation needs no device.
#
# Usage: build-synrad-service.sh <simphony-src-dir> <install-prefix> <build-dir>
#
# The simphony source dir supplies only synrad_gun.h (SYNRAD_GUN_DIR), so
# the service photons are bit-identical to the synrad/synrad_g4 validation
# modes for a given seed. Recipe notes:
# - gcc, not the container's default clang (inherited from the core recipe).
# - glm/plog spack includes and GLM_ENABLE_EXPERIMENTAL passed explicitly:
#   the sphoton.h header chain needs them and the spack container does not
#   put them on default paths (see build-simphony-synrad.sh).

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SRC=${1:?simphony source dir}
PREFIX=${2:?simphony install prefix}
BUILD=${3:?build dir}
CONTAINER=/cvmfs/singularity.opensciencegrid.org/eicweb/eic_dev_cuda:nightly

BINDS=()
for d in "$SCRIPT_DIR" "$(dirname "$SRC")" "$(dirname "$PREFIX")" "$(dirname "$BUILD")"; do
    case "$d" in
        "$HOME"*|/tmp*) ;;                    # default binds
        *) BINDS+=(--bind "$d") ;;
    esac
done

apptainer exec "${BINDS[@]}" "$CONTAINER" bash -c "
set -euo pipefail
export CC=/usr/bin/gcc CXX=/usr/bin/g++
GLM_INC=\$(ls -d /opt/software/linux-x86_64_v2/glm-*/include | head -1)
PLOG_INC=\$(ls -d /opt/software/linux-x86_64_v2/plog-*/include | head -1)
cmake -S '$SCRIPT_DIR/synrad-service' -B '$BUILD' \
    -DCMAKE_PREFIX_PATH='$PREFIX' \
    -DCMAKE_INSTALL_PREFIX='$PREFIX' \
    -DSYNRAD_GUN_DIR='$SRC/examples/synrad' \
    -DSIMPHONY_SRC_DIR='$SRC' \
    -DCMAKE_CXX_FLAGS=\"-I\$GLM_INC -I\$PLOG_INC -DGLM_ENABLE_EXPERIMENTAL\"
cmake --build '$BUILD' -j4
cmake --install '$BUILD'
"
echo "installed: $PREFIX/bin/synrad_service"
