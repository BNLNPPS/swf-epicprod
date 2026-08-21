# Volunteer-class GPU computing under PanDA

Plan of record for running the GPU optical-photon simulation
(Simphony, OptiX-based) on NVIDIA RTX hosts outside the lab
perimeter, under PanDA. The target scale is community-internal:
O(10) GPU machines contributed by collaborators would constitute a
meaningful resource. No public volunteer phase is planned.

Simphony's applications and measured performance — Cherenkov
detectors (pfRICH, dRICH, hpDIRC) and synchrotron-radiation X-ray
transport — are presented in [Optical photon and X-ray simulation
on GPU for EIC (Galgoczi, BNL EIC group meeting, August
2026)](https://docs.google.com/presentation/d/1C__dMS2L-lwcW_p3WcNr3CaZf5mK8XwIK8z6_nzoqkc/).
Synchrotron-radiation background transport is the priority workload
for this track: X-ray propagation on one RTX 4090 runs about 3000
times faster than a production CPU thread, freeing millions of CPU
core-hours per year, and its vacuum-and-wall transport driven by
file-fed photon arrays matches the coprocessor contract below
([simphony `synrad`
branch](https://github.com/BNLNPPS/simphony/tree/synrad)).

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

## Implemented (as of 2026-08-20)

The coprocessor work-unit layer is running. The contract between the
worker agent and the GPU executable is
[WORK_UNIT_CONTRACT.md](WORK_UNIT_CONTRACT.md) (work units are the
packets of the sections below); the chain lives in
`tools/worker/coprocessor/`: a work-unit dispatcher and worker agent
speaking HTTP, and the `synrad_service` work-unit loop holding geometry
and the OptiX context resident across units. Unit output is a pure
function of the unit spec — verified byte-identical across stream
positions, and reference-exact across platforms
([SYNRAD_VALIDATION.md](SYNRAD_VALIDATION.md)). A self-contained driver
runs a unit batch as a PanDA job payload on `BNL_NPPS_GPU`, with the
reference-set check inside the job and the verdict in the job record:
work enters through PanDA only, runs through the chain, and is
accounted end to end.

## Roadmap

Each step de-privileges the worker further, until the first worker
and a collaborator's machine are configured identically.

1. **Gateway — the pool's mediation service (devcloud).** Design: The
   gateway and the pool agent, below. It accretes in steps, each
   usable on its own:
   the unit service (the work-unit dispatcher served at a public
   address; first deployment serves owned machines, with trust by
   ownership); device identity (enrollment, per-device tokens,
   revocation as the whole security lifecycle); the S3 presigner
   (uploads by gateway-issued presigned PUT, time-limited and
   single-object — the AWS keys leave the worker); and the agent tick
   endpoint below. PanDA mediation for full workers (the robot
   identity held gateway-side) completes the untrusted-machine
   posture: a worker then carries only its revocable device token.
2. **The pool agent.** One agent for every pool machine, identical on
   lab and volunteer hosts: a single static binary speaking outbound
   HTTPS to the gateway and nothing else — no broker, no inbound
   port, no held connections. Its single verb is the tick. Running,
   the tick carries telemetry and unit progress out; idle, the same
   tick is the request for work; in both states the reply carries
   unit leases and commands (fire pilots, pause), with cadence the
   only knob. On lab hosts the fire-pilots command drives pilot
   passes through the existing per-GPU lock machinery, retiring the
   polling launcher loop.
3. **Worker bundle.** Launcher, pass script, git configuration, and
   the enrollment step packaged as an installable kit for any NVIDIA
   RTX Linux host. The bundle is the volunteer landing.
4. **Windows port.** Complete: the four core packages build native
   with MSVC/CUDA/OptiX, and the Windows service executable
   reproduces the Linux reference set exactly
   ([SYNRAD_VALIDATION.md](SYNRAD_VALIDATION.md),
   [simphony PR #438](https://github.com/BNLNPPS/simphony/pull/438)).
   The remaining Windows work belongs to the worker prototype:
   service soak under the WDDM watchdog, and packaging (the
   executable, its runtime libraries, and the pool agent as the
   participation package).
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
streamed in small units so an interrupted worker loses at most
the unit in flight (see Preemption and checkpointing below). The
work-unit layer now carries this: units are the streamed,
interruption-bounded quantum, idempotent by construction — an
abandoned unit is simply reprocessed
([WORK_UNIT_CONTRACT.md](WORK_UNIT_CONTRACT.md)).

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
drive the GPU propagation from persisted files — a geometry bundle
and input arrays — with no Geant4 in the process. The containment
boundary is proven on the supported platform in two workloads. The
optical trial runs `CSGOptiXServiceTest` against a raindrop geometry
bundle and captured Cerenkov gensteps
(`tools/worker/capture-simphony-trial-inputs.sh`,
`run-simphony-containment.sh`). The synchrotron-radiation
containment runs the `synrad_service` executable
(`tools/worker/synrad-service/`, `run-synrad-containment.sh`)
against the persisted tessellated SynradBenchmark chamber and an
input photon array; it reproduces the integrated run's counts and
passes the statistical comparison against the Geant4 reference.

The **Windows reference set** is the synchrotron-radiation one,
assembled by `tools/worker/make-synrad-refset.sh` after a
file-driven replay gate: the chamber geometry bundle and the input
photon array together with the hit records they produce on Linux,
archived with SHA-256 checksums as the comparison baseline.

The Windows port is then judged against that baseline: the same
executable shape built with MSVC/NVCC, fed the same geometry and
input arrays, compared on the hits. The comparison is statistical, not
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

## The gateway and the pool agent

The gateway is the pool's mediation service: the single meeting point
between the lab side, where work originates, and the workers, wherever
they sit. It is never an intake — work enters the system only through
PanDA, and every work unit the gateway serves carries the identity of
the PanDA job that produced it. On its lab side the gateway is an
ordinary platform service: the production ops agent messages it,
monitoring reads it, and the platform's message bus ends there. On its
worker side it speaks nothing but HTTPS request-response under device
tokens; no worker anywhere speaks a platform protocol.

The gateway has four functions. It holds the device registry: a machine
enrolls once and receives a per-device token, and revoking that token
is the entire security lifecycle for the machine. It serves work units
under the contract of [WORK_UNIT_CONTRACT.md](WORK_UNIT_CONTRACT.md):
the driver of a PanDA job enqueues units, workers lease them and return
hits and unit records, lease expiry re-queues abandoned units, and unit
idempotency makes both reprocessing and duplicate-dispatch verification
exact. It issues presigned S3 uploads — time-limited, single-object —
so bulk results travel directly to lab-side storage while AWS
credentials never reach a worker. And it answers the pool agent's tick.

The pool agent is the worker-side counterpart, and there is exactly one
of it: the same agent on lab and volunteer machines, a single static
binary speaking outbound HTTPS to the gateway and nothing else — no
message broker, no inbound port, no held connections. A stranger's
machine behind NAT and a firewall runs it unchanged, which is the
untrusted-worker doctrine applied to the agent itself: the first worker
and a volunteer's machine converge on identical configuration. The
agent's single verb is the tick. While units run, the tick carries
telemetry, unit progress, and completion manifests out; while idle, the
same tick is the request for work; in both states the reply carries
unit leases and any queued commands — fire pilots, pause — so command
delivery needs no channel of its own and its latency is the tick
cadence, the protocol's only knob. On lab hosts the fire-pilots command
starts pilot passes through the existing per-GPU lock machinery,
retiring the polling launcher loop; on volunteer hosts the command set
implements the preemption and scheduling policy of the sections below.

The public face is devcloud. Heavy back-end functions can sit on lab
GPU hardware behind the established reverse-tunnel pattern, keeping the
public host thin. The gateway accretes in stages, each usable on its
own: the unit service alone first — the running loopback chain moved to
a public address, serving owned machines with trust supplied by
ownership — then device identity, then the presigner, then the tick.
Nothing built for an earlier stage is discarded by a later one.

Gateway state — the roster, leases, heartbeats, and per-unit records —
is the source the pool monitoring pages and history views render.
Workers never talk to the monitor; the gateway relays their state to
the platform.

## Preemption and checkpointing

A volunteer machine is returned to its owner the moment they want
it; among friends this is a hard requirement. The coprocessor
model meets it without checkpoint machinery, because the
checkpoint is the executable and the geometry bundle — immutable
files already on the worker's disk. No dynamic state is ever
saved. Acquiring the GPU costs a few seconds to load the geometry
and build the acceleration structures; the workflow then consumes
a succession of modestly sized work packets.

Preemption is instant because the packet in flight is abandoned.
GPU cycles return within one kernel launch: launches are
millisecond-scale (and bounded on Windows regardless by WDDM
timeout detection), so compute yields at the speed of a context
switch. GPU memory returns by terminating the worker process,
well under a second, freeing the geometry and photon buffers for
the owner's use. The abandoned packet re-queues through the
event-range accounting on the server, and packet granularity
keeps the loss trivial. The seconds of reload and rebuild are
paid when the owner leaves the machine, not when they return.

A local cache of fetched packets keeps the GPU fed independently
of network latency and variability, and draws on the network in a
smooth, modest stream rather than bursts. Cached packets lost to
a preemption re-queue when their leases lapse, exactly as the
in-flight one does. Packet size is therefore bounded three ways:
large enough to amortize transfer and dispatch overheads, small
enough to bound the loss from an interruption, and small enough
that abandoning one is trivial. All three point to the same
seconds-scale packet.

Preemption triggers are owner input, a fullscreen application,
and a manual pause control, with conservative defaults: any sign
of the owner takes the worker off the GPU.

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
