#!/bin/bash
# Assemble the synchrotron-radiation Windows reference set from containment
# artifacts (VOLUNTEER_GPU_PLAN.md, synchrotron-radiation track): persisted
# chamber geometry, input photon array, and the hit records from every mode,
# with SHA-256 checksums and a README, packed into a dated tarball.
#
# Before assembly the coprocessor contract is exercised end to end: a
# file-driven synrad_service replay (photons from the .npy artifact, not the
# gun) must reproduce the recorded hit and reflection counts exactly.
# Requires a GPU for that step (--nv); device 0 pinned.
#
# Usage: make-synrad-refset.sh <simphony-src-dir> <install-prefix> <trial-dir> <refset-dir> <simphony-commit>

set -euo pipefail

SRC=${1:?simphony source dir}
PREFIX=${2:?simphony install prefix}
TRIAL=${3:?trial dir}
REFSET=${4:?refset output dir}
COMMIT=${5:?simphony synrad-branch commit hash}
CONTAINER=/cvmfs/singularity.opensciencegrid.org/eicweb/eic_dev_cuda:nightly

for f in "$TRIAL/geom/CSGFoundry" "$TRIAL/synrad_service_inphoton.npy" \
         "$TRIAL/synrad_service_hits.npy" "$TRIAL/synrad_hits.npy" \
         "$TRIAL/synrad_g4_hits.npy"; do
    [ -e "$f" ] || { echo "missing trial artifact: $f" >&2; exit 1; }
done
[ -d "$REFSET" ] && { echo "refset dir exists: $REFSET" >&2; exit 1; }
mkdir -p "$REFSET"/{geometry,inphoton,hits-service,hits-integrated,hits-g4,hits-replay-filefed}

BINDS=()
for d in "$(dirname "$SRC")" "$(dirname "$PREFIX")" "$(dirname "$TRIAL")" "$(dirname "$REFSET")"; do
    case "$d" in
        "$HOME"*|/tmp*) ;;                    # default binds
        *) BINDS+=(--bind "$d") ;;
    esac
done

apptainer exec --nv "${BINDS[@]}" "$CONTAINER" bash -c "
set -euo pipefail
export LD_LIBRARY_PATH='$PREFIX/lib:$PREFIX/lib64'\${LD_LIBRARY_PATH:+:\$LD_LIBRARY_PATH}
export CUDA_VISIBLE_DEVICES=0
export GEOM=synrad
export synrad_CFBaseFromGEOM='$TRIAL/geom'

echo '== file-driven replay: photons from the .npy artifact =='
'$PREFIX/bin/synrad_service' -i '$TRIAL/synrad_service_inphoton.npy' \
    -o '$REFSET/hits-replay-filefed' 2>&1 | grep '^synrad-service:'

python3 - <<'PY'
import numpy as np
rec = np.load('$TRIAL/synrad_service_hits.npy')
rep = np.load('$REFSET/hits-replay-filefed/synrad_service_hits.npy')
BR = 0x1 << 10
nr_rec = int((np.ascontiguousarray(rec[:,3,3]).view(np.uint32) & BR != 0).sum())
nr_rep = int((np.ascontiguousarray(rep[:,3,3]).view(np.uint32) & BR != 0).sum())
print(f'recorded: {len(rec)} hits {nr_rec} reflected | file-fed replay: {len(rep)} hits {nr_rep} reflected')
assert len(rec) == len(rep) and nr_rec == nr_rep, 'file-fed replay does not reproduce the recorded counts'
print('file-driven replay reproduces the recorded counts')
PY
"

cp -r "$TRIAL/geom/." "$REFSET/geometry/"
cp "$TRIAL/synrad_service_inphoton.npy" "$REFSET/inphoton/"
cp "$TRIAL/synrad_service_hits.npy" "$REFSET/hits-service/"
cp "$TRIAL/synrad_hits.npy" "$REFSET/hits-integrated/"
cp "$TRIAL/synrad_g4_hits.npy" "$REFSET/hits-g4/"

cat > "$REFSET/README.md" <<EOF
# Simphony synchrotron-radiation containment reference set

Reference set for the Windows port of the Simphony GPU synchrotron-radiation
transport (swf-epicprod docs/VOLUNTEER_GPU_PLAN.md). Produced on a Linux
host with an NVIDIA RTX 4090 by running the synrad_service executable
(swf-epicprod tools/worker/synrad-service/) standalone against a persisted
CSGFoundry geometry bundle and an input photon array, with no Geant4
present in the process.

The workload is the SynradBenchmark tunnel (a 50 m Cu chamber: drift,
10 mrad arc, drift, fused into one closed tessellated solid) transporting
500000 soft X-ray photons (0.3-19.4 keV, 7 mrad pencil beam, seed 42) with
the reflect-or-absorb grazing-incidence Cu reflectivity model of
qsim::propagate_gamma.

Provenance:

- simphony synrad branch commit $COMMIT
- container /cvmfs/singularity.opensciencegrid.org/eicweb/eic_dev_cuda:nightly
- build recipes: tools/worker/build-simphony-synrad.sh,
  tools/worker/build-synrad-service.sh
- validation: tools/worker/run-synrad-trial.sh (GPU vs Geant4 reference,
  six statistical checks, PASS)
- containment: tools/worker/run-synrad-containment.sh (no Geant4/G4CX in
  the process image; persisted-geometry replay reproduces the integrated
  run's counts and passes the same checks against the Geant4 reference)

Contents:

- geometry/ — persisted CSGFoundry bundle and origin.gdml (tessellated
  SynradBenchmark chamber, 1252 facets)
- inphoton/synrad_service_inphoton.npy — the 500000 input photons,
  (N,4,4) float32 sphoton, energy in keV in the wavelength slot
- hits-service/ — wall-absorption hits from the standalone gun-fed run
- hits-replay-filefed/ — hits from the file-driven replay (photons read
  from inphoton/), reproducing the service counts exactly
- hits-integrated/ — hits from the integrated GDML-loading GPU run
- hits-g4/ — the Geant4 reference record (synrad_g4, same photons)

The input photon array, not the gun, is the cross-platform contract:
std::mt19937 is portable but the distribution implementations are not
(libstdc++ vs MSVC). A Windows build is fed inphoton/ and geometry/ and
judged against hits-service/ statistically — hit counts, reflected
fraction, absorption-point marginals, reflected-energy spectrum — with
the tolerances of optiphy/ana/synrad_test.py; bitwise agreement is not
expected across compilers or OptiX traversal orders.

Array schemas are documented in simphony docs/inputs-outputs.md.
MANIFEST.sha256 lists SHA-256 checksums for every file.
EOF

( cd "$REFSET" && find . -type f ! -name MANIFEST.sha256 -print0 | sort -z \
    | xargs -0 sha256sum > MANIFEST.sha256 )

TARBALL=$REFSET.tar.gz
tar -C "$(dirname "$REFSET")" -czf "$TARBALL" "$(basename "$REFSET")"
sha256sum "$TARBALL"
echo "refset: $REFSET"
echo "tarball: $TARBALL"
