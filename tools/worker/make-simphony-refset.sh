#!/bin/bash
# Assemble the Windows reference set from containment-trial artifacts
# (VOLUNTEER_GPU_PLAN.md, the Linux containment trial): geometry bundle,
# genstep inputs, and hit outputs, with SHA-256 checksums and a README,
# packed into a dated tarball.
#
# Usage: make-simphony-refset.sh <trial-dir> <refset-dir> <simphony-commit>

set -euo pipefail

TRIAL=${1:?trial dir}
REFSET=${2:?refset output dir}
COMMIT=${3:?simphony commit hash}
EVDIR=ALL0_no_opticks_event_name/A000

[ -d "$REFSET" ] && { echo "refset dir exists: $REFSET" >&2; exit 1; }
mkdir -p "$REFSET"/{geometry,gensteps,hits-integrated,hits-replay}

cp -r "$TRIAL/geom/." "$REFSET/geometry/"
cp "$TRIAL/events/$EVDIR/genstep.npy" "$REFSET/gensteps/"
cp "$TRIAL/events/$EVDIR/hit.npy" "$REFSET/hits-integrated/"
cp "$TRIAL/replay/$EVDIR/hit.npy" "$REFSET/hits-replay/"

cat > "$REFSET/README.md" <<EOF
# Simphony Linux containment trial reference set

Reference set for the Windows port of the Simphony GPU optical simulation
(swf-epicprod docs/VOLUNTEER_GPU_PLAN.md, "The Linux containment trial and
the Windows reference set"). Produced on a Linux host with an NVIDIA RTX
4090 by running CSGOptiXServiceTest standalone against persisted inputs,
with no Geant4 present in the process.

Provenance:

- simphony main commit $COMMIT
- container /cvmfs/singularity.opensciencegrid.org/eicweb/eic_dev_cuda:nightly
- build recipe: swf-epicprod tools/worker/build-simphony-core.sh
- input capture: tools/worker/capture-simphony-trial-inputs.sh (raindrop
  DD4hep example: one event, 10 MeV electron in water, Cerenkov gensteps)
- standalone replay: tools/worker/run-simphony-containment.sh

Contents:

- geometry/ — persisted CSGFoundry bundle and origin.gdml (raindrop geometry)
- gensteps/genstep.npy — 110 Cerenkov gensteps from the captured event
- hits-integrated/hit.npy — 9269 hits from the integrated DD4hep run
- hits-replay/hit.npy — 9269 hits from the standalone replay

The comparison basis is statistical, not bitwise. The integrated and replay
hit arrays have identical counts and matching position, time, and wavelength
moments, and differ bitwise even on the same GPU because OptiX traversal
order is nondeterministic. A Windows build is judged against hits-replay on
hit counts, distributions, and stated tolerances.

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
