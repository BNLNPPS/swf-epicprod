# AdePT audit

Implications of adding AdePT — GPU electromagnetic transport for
electrons, positrons, and gammas — to the GPU-executable approach of
[VOLUNTEER_GPU_PLAN.md](VOLUNTEER_GPU_PLAN.md), audited in advance of
the AdePT+Simphony integration under way upstream. The audit covers the
build environment, the measured footprint, the encapsulation path, and
platform reach; it does not duplicate the integration itself.

## Environment

The `eic_dev_cuda` container ships AdePT: the full library set
(`libadept_transport`, `libadept_g4integration`, shared and static) is
installed in the container's environment view, alongside the complete
dependency chain in its spack tree — VecGeom 2.0.0 with CUDA support,
G4HepEm 20251114, VecCore 0.8.2, Geant4 11.4.1, CUDA 12.x, gcc 14
(C++20). AdePT master (github.com/apt-sim/AdePT) configures and builds
clean against that tree with no source changes:

```bash
cmake -S AdePT -B build \
  -DCMAKE_PREFIX_PATH="<vecgeom>;<g4hepem>;<veccore>" \
  -DCMAKE_CUDA_ARCHITECTURES=89 -DCMAKE_BUILD_TYPE=Release \
  -DADEPT_BUILD_EXAMPLES=ON -DCMAKE_CXX_FLAGS="-I<clhep>/include"
```

The consequence: an AdePT+Simphony executable deploys wherever the
container already goes — every Linux full worker in the pool, with no
new distribution machinery.

Two build notes:

- AdePT's Geant4 feature probes (`check_cxx_source_compiles` against
  `G4Track.hh` and the tracking-manager interface) do not carry CLHEP's
  include path. On spack stacks, where CLHEP is external to Geant4, the
  probes fail and the examples are silently disabled; the
  `CMAKE_CXX_FLAGS` addition above is the workaround, and a
  `CMAKE_REQUIRED_INCLUDES` entry upstream is the fix. Reported to the
  integration effort.
- Because the container ships AdePT, a from-source build inside it must
  keep its own libraries first on `LD_LIBRARY_PATH`; the container's
  copies otherwise shadow the build (a symbol-lookup failure on the
  fatbin registration symbols is the signature).

## Measured footprint

Example1 (the shipped Geant4 application with AdePT offload), CMS 2018
GDML, one event of 200 × 10 GeV electrons, GPU transport in all
regions, 1M track slots and 1M hit slots, RTX 4090:

| | |
|---|---|
| Peak device memory | 3.0 GB |
| Host memory | 1.4 GB |
| Wall time | 35 s, dominated by geometry and physics initialization |

Slot budgets are the device-memory knob. Co-residency arithmetic for a
combined executable: AdePT at this configuration plus the synrad
service as measured (~3.4 GB) is ~6.4 GB on one card — comfortable on
24 GB lab cards, feasible on 8–12 GB volunteer cards with reduced slot
budgets on both sides. The SR beampipe geometry is far lighter than the
CMS geometry used here.

## Encapsulation

The containment pattern of the synrad service transfers to AdePT: a
worker-side AdePT+Simphony executable needs no Geant4 in the process.
The seams exist upstream:

- VecGeom loads GDML natively (`libvgdml`) — geometry construction
  without Geant4, from the same GDML source that produces the
  CSGFoundry bundle.
- G4HepEm physics data serializes to JSON (`G4HepEmDataJsonIO`):
  initialized from Geant4 once, lab-side, per edition — megabytes, not
  the gigabyte-scale Geant4 data sets — and reloaded standalone.
- The Geant4-free driving of AdePT's transport loop is precisely the
  AdePT+Simphony integration in progress upstream.

The geometry edition bundle grows accordingly: GDML and the HepEm JSON
travel alongside the CSGFoundry bundle, all derived from one source.
Geant4 remains lab-side, for per-edition capture and for validation
references.

## Platform reach

Geant4 supports Windows natively; that implies nothing about VecGeom,
which is an optional Geant4 backend. A survey found no public Windows
build of VecGeom: no upstream issues mentioning Windows or MSVC, no
forks carrying a port, no vcpkg or conda packaging. VecCore, the
foundation layer, ships an `MSVC.cmake` in its official tree. The
status is therefore unattempted rather than unattemptable — the same
grade the Simphony core packages had before their port — and the
remaining Windows surface for a volunteer-side AdePT+Simphony
executable is VecGeom plus AdePT's device and host code under
MSVC-hosted nvcc. Deferred until volunteer-side electron generation is
wanted; nothing in the plan requires it.

## Generation tiers

The audit confirms the two-tier generation picture:

- **AdePT+Simphony** is the full-fidelity generator, running where the
  stack lives — lab and institutional Linux workers, via the container
  that already ships every dependency. Input remains a compact
  primaries specification: the zero-input property of the work-unit
  design is preserved.
- **The source table** (per-edition trajectory and curvature table,
  sampled on the GPU inside the Simphony-only executable) is the
  volunteer-grade generator, requiring none of the AdePT stack.
  AdePT+Simphony is its natural validation reference. Genstep
  resampling — reusing a finite captured genstep set with fresh random
  numbers — risks undersampling distribution tails; closed-form table
  sampling has no finite set to exhaust.

## Open question

AdePT's GPU random-number generator is RANLUX++ (curand serves the
Simphony side, with bitwise cross-platform reproduction demonstrated).
Whether AdePT's per-track determinism survives across platforms and
compilers is unverified; until it is tested, duplicate-dispatch
verification for electron-fed work units cannot assume exactness.
