# Volunteer-class GPU computing under PanDA

Plan of record for running the GPU optical-photon simulation
(Simphony, OptiX-based) on NVIDIA RTX hosts outside the lab
perimeter, under PanDA. The target scale is community-internal:
O(10) GPU machines contributed by collaborators would constitute a
meaningful resource. No public volunteer phase is planned.

The guiding constraint is that a worker host is untrusted. Every
design choice follows from it: a credential on a worker must be
worth no more than that worker's participation — scoped to its own
uploads, individually revocable, useless beyond its slot. Outputs
are small (logs, hit summaries); bulk data stays on lab storage.

## Implemented (as of 2026-08-14)

The first worker is the NPPS GPU server npps0 (2× RTX 4090), a host
outside the SCDF perimeter — deliberately treated as a strange
machine in the wild, so what works there transfers to any
collaborator's box.

- `BNL_NPPS_GPU` PanDA queue; jobs dispatch through the standard
  server path and run in the `eic_dev_cuda` CVMFS container with GPU
  passthrough (`--nv`). The Simphony raindrop test passes on both
  GPUs under PanDA dispatch.
- Pull-mode pilot under a launcher loop (`NPPS0_WORKER.md`): no
  Harvester, no compute element. Service restarts never kill a
  running pass; configuration deploys take effect at pass boundaries
  with no service action.
- Git-sourced configuration: the queue's pilot-side behavior
  (`queuedata.json`) and the storage catalog
  (`agis_ddmendpoints.json`) are version-controlled files applied
  per pass. CRIC holds only the queue's existence.
- S3 stage-out (`docs/DEVCLOUD_STAGEOUT.md`): job logs go to a
  devcloud S3 bucket with lifecycle expiry, via the pilot's native
  s3 copytool. Lab dCache doors are unreachable from outside the
  perimeter; the bucket is the perimeter-external destination.

Credential posture on npps0 is the trusted-machine tier: production
token, proxies, and an AWS profile held on the host under our
administration. The roadmap below removes them one class at a time.

## Roadmap

Each step de-privileges the worker further, until the first worker
and a collaborator's machine are configured identically.

1. **Gateway v0 — uploads (devcloud).** A small authenticated
   service holding a device registry and an S3 presigner. Workers
   enroll once and receive a device token; uploads use
   gateway-issued presigned PUT URLs — time-limited, single-object.
   The AWS keys leave the worker. The s3 copytool data path is
   unchanged; only the credential source changes.
2. **Gateway v1 — PanDA mediation.** The gateway proxies job
   acquisition and status updates, holding the robot identity
   internally; workers authenticate to the gateway with their device
   token. The PanDA proxy leaves the worker. A worker then carries
   only its revocable device token — the full untrusted-machine
   posture, while running production-shaped jobs.
3. **Worker bundle.** Launcher, pass script, git configuration, and
   the enrollment step packaged as an installable kit for any NVIDIA
   RTX Linux host. The bundle is the volunteer landing.
4. **Windows port.** RTX-class Windows PCs are where the volunteer
   resource largely lives. OptiX is unsupported under WSL2, so the
   route is a native Windows build of the worker executable under
   the coprocessor model below; the container-on-Windows path is not
   pursued. This is the one open R&D item in the plan, and nothing
   earlier depends on it.
5. **Fleet operation.** O(10) machines; a tailnet as the
   community-internal transport so workers reach the gateway over
   authenticated, non-public paths; per-device revocation as the
   whole security lifecycle.

Mechanisms available in PanDA for this track — secrets delivery,
object-store handling, fine-grained dispatch, brokerage by hardware
class — together with the monitoring work needed to make GPU worker
jobs legible, are inventoried in `docs/PANDA_CAPABILITIES.md`.

High on the engineering list: retiring the `runGen` transform. It is
analysis-era scaffolding — URL-encoded payload strings, client-side
substitution devices, output plumbing built around grid datasets —
and every one of its assumptions has cost integration effort here. A
purpose-built worker executor (fetch payload spec, run in container,
place outputs for object-store stage-out) is small, and it is the
natural sibling of the gateway work: both replace grid-era
scaffolding with pieces sized to this system.

Interruption robustness follows the event-service model: work is
streamed in small units so a closed laptop or powered-off PC loses
at most the unit in flight. The current single-job smoke tests do
not exercise this; it enters with the first production-shaped
streaming workload.

## The Windows coprocessor model

The ePIC software stack is Linux-only and remains so. The Windows
worker therefore runs none of it — no Geant4, DD4hep, ROOT, CVMFS,
container runtime, or pilot. It runs a single native executable
built from Simphony's four core packages (SysRap, CSG, QUDArap,
CSGOptiX) and acts as a remote GPU coprocessor: a Linux application
produces its inputs and consumes its outputs.

The isolation boundary exists in Simphony today. `CSGOptiXService`
loads a persisted CSGFoundry geometry — a directory of typed arrays
carrying solids, materials, and optical surface properties — and
exposes one operation: gensteps in, hits out, both as .npy arrays.
The worker executable loads the geometry bundle once per detector
edition, holds the OptiX context resident, and loops over work
units: read a genstep array, propagate on the GPU, write the hit
array. Runtime dependencies on the worker are the NVIDIA driver
(which carries the OptiX runtime) and the CUDA runtime library.

No container infrastructure is required on the worker. The
container on Linux carries the full ePIC stack; the coprocessor
runs none of that stack, and Docker on Windows would in any case
route through WSL2 or a Linux virtual machine, where OptiX is
unsupported. Each function the container serves elsewhere is
covered without one: the environment is pinned by shipping the
executable and its libraries as a versioned bundle; software
distribution is gateway-served versioned downloads over HTTPS in
place of CVMFS; isolation is a single unprivileged user-space
process exchanging arrays over HTTPS under a revocable device
token, removed by deleting its folder; and correctness is judged
against the Linux reference set statistically, with sampled
duplication across the fleet in operation. The volunteer install
is the participation package plus a current GeForce driver.

Batching is essential to the coprocessor scheme, not an
optimization: a work package must carry enough events that GPU
processing amortizes the overheads at either end — transfer,
dispatch, and stage-out round trips that are far larger for a remote
worker than for a local one. Package size is the central tuning
knob, bounded below by amortization and above by the loss a single
interruption may cause. Event attribution for batched propagation —
gensteps accumulated across events and propagated in one GPU pass —
is solved by Simphony's EventBatcher
(`event-batching` branch, built against the original simg4ox
integration and predating the DD4hep layer): hits map back to their
originating events through the seed buffer, hit index to photon to
genstep to event ID. The later DD4hep integration
(`dd4hepplugins/OpticsEvent.cc`) has not yet absorbed the mechanism
and injects hits only in per-event mode, and
with more than one sensitive detector registered it injects into the
first — routing by sensor identity is not yet implemented there.
Both are integration adoption on the Linux side, not platform work;
the coprocessor contract carries the same seed and genstep
bookkeeping in its arrays.

### The Linux containment trial and the Windows reference set

The first step toward the Windows build runs entirely on Linux:
build the four core packages alone, with no Geant4 or DD4hep in the
build, and drive `CSGOptiXService` from persisted files — a real
detector geometry bundle and genstep arrays captured from a standard
ePIC simulation — producing hit arrays with no Geant4 in the
process. This proves the containment boundary on the supported
platform, and its artifacts are the **Windows reference set**: the
geometry bundle and genstep inputs together with the hit outputs
they produce on Linux, archived as the comparison baseline.

The Windows port is then judged against that baseline: the same
executable shape built with MSVC/NVCC, fed the same geometry and
gensteps, compared on the hits. The comparison is statistical, not
bitwise — compiler floating-point differences and OptiX's
nondeterministic traversal order preclude bit-identical output, so
agreement is judged on hit counts, distributions, and stated
tolerances. The same comparison machinery later serves sampled
duplication across the fleet.

The port work itself is mechanical: a core-only build option (the
top-level build currently always includes the Geant4-dependent
packages), Windows export declarations in place of the gcc
visibility attributes, and replacement of POSIX usages (unistd,
/proc, popen) in the utility layer.

## Worker electricity cost

A high-end card (RTX 4090 class) draws roughly 550 W at the wall
under full load, system overhead and supply losses included;
midrange RTX cards draw roughly 300 W. Ray-tracing workloads
typically draw below gaming maximum, so these are ceilings. At
US residential rates a machine run flat out costs $1–2 per day
depending on card and local rate; likely part-time,
non-aggressive participation costs tens of cents per day.

Per unit of work the cost is negligible: warm-event throughput
near 4M photons/s at 550 W is roughly 0.1 mJ per propagated
photon, so participation hours, not the workload, set the
volunteer's bill. The participation package will carry two cost
controls: a power cap (RTX cards power-limited to about 70% lose
about 10% of ray-tracing throughput) and hour and idle-time
scheduling.

## Components and ownership

| Piece | Where | Status |
|---|---|---|
| Queue definition | CRIC (existence only) | live |
| Queue behavior | `tools/npps0/config/queuedata.json` | live |
| Storage catalog | `tools/npps0/config/agis_ddmendpoints.json` | live |
| Stage-out target | devcloud S3 (`DEVCLOUD_STAGEOUT.md`) | live |
| Worker runtime | `tools/npps0/` launcher + pass | live |
| Gateway | devcloud | planned (v0 next) |
| Worker bundle | packaging of the above | planned |
| Windows worker executable | native build of the Simphony core packages | planned |
