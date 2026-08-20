# Synchrotron-radiation GPU transport: validation record

Validation results for the Simphony synchrotron-radiation GPU transport
(simphony [`synrad` branch](https://github.com/BNLNPPS/simphony/tree/synrad),
base commit `25eafb63a`) on Linux and Windows, under the coprocessor model
of [VOLUNTEER_GPU_PLAN.md](VOLUNTEER_GPU_PLAN.md). The build and run
recipes are committed in `tools/worker/` (Linux) and `tools/worker/windows/`
(Windows); the acceptance procedure is defined in
[WINDOWS_WORKER.md](WINDOWS_WORKER.md).

## Linux validation

Host: npps0 (RTX 4090, [NPPS0_WORKER.md](NPPS0_WORKER.md)), `eic_dev_cuda`
container, Release build.

**Physics, GPU vs Geant4.** 500,000-photon pencil beam, seed 42, the
SynradBenchmark copper chamber. All six checks of simphony
`optiphy/ana/synrad_test.py` pass: reflected-at-least-once fraction
0.7243 (GPU) vs 0.7244 (Geant4), χ²/ndf 1.15, 1.03, 0.93 on the
absorption-point marginals and 0.36 on the reflected-energy spectrum.

**Performance, upstream methodology.** `examples/synrad/speedup.sh` run
unmodified against the Release install, alongside the branch's published
numbers (measured on the same host class):

| | µs/photon (this validation) | published |
|---|---|---|
| Geant4 single-thread, analytic CSG | 9.62 | 8.40 |
| Geant4 single-thread, meshed drifts (SR electrons) | 168.4 | 157 |
| GPU, coarse mesh (1252 facets) | 0.0849 | 0.075 |
| GPU, fine mesh (~25k facets) | 0.0900 | 0.079 |
| **Production ratio** | **113×** | **112×** |
| **CAD-layout ratio** | **1870×** | **~1980×** |

The fine mesh costs Geant4 17.5× and the GPU 1.06×. Physics counts are
unchanged between the optimized and unoptimized builds.

**Containment.** The service executable (`tools/worker/synrad-service/`)
runs the same transport with no Geant4 in the process: geometry from a
persisted CSGFoundry bundle, photons from a file-fed array. It reproduces
the integrated run's counts exactly (500,000 absorbed, 362,156 reflected)
and passes the same six checks against the Geant4 reference. The
**reference set** — geometry bundle, input photon array, and the hit
records they produce, with SHA-256 manifest — is assembled from this run
by `tools/worker/make-synrad-refset.sh` and is the cross-platform
comparison baseline.

## Windows validation

Host: a cloud-hosted Windows 11 machine, RTX A4500 (20 GB), driver
565.90, WDDM. Toolchain: MSVC v143, CUDA 12.6, OptiX SDK 8.1. The build
follows the committed recipes in `tools/worker/windows/`.

**Acceptance against the reference set.** The Windows-built
`synrad_service` executable, fed the reference set's geometry bundle and
input photon array, matches the recorded Linux hits on all six checks
with χ²/ndf 0.00. The integer counts are identical: 500,000 absorbed,
362,156 reflected, 11,316 cap hits — exceeding the statistical agreement
the acceptance requires (bitwise agreement is not expected across
compilers and OptiX traversal orders, and is not the criterion). The
result is identical across three build variants. Transport cost:
0.24 µs/photon on the A4500.

**Scope.** The acceptance covers the file-fed transport path, which is
the coprocessor contract. The generator path is excluded by design:
`std::mt19937` distribution implementations differ between libstdc++ and
MSVC, so identical seeds produce different photon sets; file-fed arrays
carry cross-platform reproducibility. Long-run stability, multi-event
sequences, and operation near the WDDM watchdog limit are not covered
here; they belong to the worker prototype.

## Port changes upstream

The Windows port of the four core packages (SysRap, CSG, QUDArap,
CSGOptiX) is submitted as
[simphony PR #438](https://github.com/BNLNPPS/simphony/pull/438) — four
commits on base `25eafb63a`: build configuration, export headers, source
port, static-build fixes. Two of the fixes apply beyond Windows and are
inert in the shared Linux build:

- Directory-existence checks no longer rely on `ifstream` opening a
  directory, a glibc-specific behavior.
- `SLOG_INIT` no longer permits a logger to add itself as an appender,
  which recursed to stack overflow in fully static builds.
