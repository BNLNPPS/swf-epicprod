# The Windows worker

Windows-generic documentation for the Simphony Windows worker under the
coprocessor model of [VOLUNTEER_GPU_PLAN.md](VOLUNTEER_GPU_PLAN.md):
platform requirements, development toolchain, build target, and the
acceptance procedure. Machine-specific records live with each host; the
first Windows host's record is the tjai entry `shadow-pc_details`.

## Platform requirements

- 64-bit Windows 10/11 with an NVIDIA RTX-class GPU. RT cores carry the
  OptiX ray-tracing acceleration the workload is built on.
- NVIDIA driver 535 or later (the OptiX 8.0 runtime ships inside the
  driver; 555+ carries 8.1). Volunteer machines need no SDK and no CUDA
  toolkit — the worker bundle ships the executable and its runtime
  libraries.
- WDDM driver model: GPU kernel launches run under the display watchdog
  (about 2 s by default). The packet design keeps launches at millisecond
  scale, well inside it.
- No container runtime and no WSL2. OptiX is unavailable under WSL2 GPU
  paravirtualization, which is the basis of the plan's no-container
  decision for workers.

## Development toolchain

The port is developed with (volunteers receive binaries, none of this):

- Visual Studio 2022 Build Tools, C++ workload (MSVC v143, Windows SDK)
- CMake 3.22+
- CUDA Toolkit 12.x compatible with the host driver
- OptiX SDK 8.x headers (NVIDIA developer download)
- Python 3.12 with numpy, for the acceptance comparison

## Build target

The four Simphony core packages — SysRap, CSG, QUDArap, CSGOptiX — with no
Geant4 and no DD4hep in the build, plus the synchrotron-radiation service
executable (`tools/worker/synrad-service/`) as the acceptance workload.
Known port areas, from the Linux-side assessment:

- a core-only top-level build configuration (the stock build always
  includes the Geant4-dependent packages u4, g4cx, src);
- Windows export declarations (`__declspec`) in place of the gcc
  visibility attributes in the `*_API_EXPORT.hh` headers;
- POSIX usage replacement in the utility layer (unistd.h, /proc, popen);
- the gamma transport mode preserved intact: `qudarap/qgxs.h`,
  `qgxs_synradg4.h`, the `QSim::setGXS` dispatch, and the triangulated
  geometry path.

## Acceptance

The synchrotron-radiation reference set (assembled by
`tools/worker/make-synrad-refset.sh` from the Linux containment run) is
the comparison baseline. A Windows build is fed the reference set's
persisted chamber geometry and input photon array and judged on its
wall-absorption hits against the recorded Linux hits, with the
statistical tolerances of simphony `optiphy/ana/synrad_test.py`: absorbed
count, reflected-at-least-once fraction, absorption-point marginals, and
the reflected-energy spectrum. Bitwise agreement is not expected across
compilers or OptiX traversal orders and is not the criterion.
