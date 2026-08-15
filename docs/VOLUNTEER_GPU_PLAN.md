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
4. **Windows port.** The same bundle on Windows via WSL2. RTX-class
   Windows PCs are where the volunteer resource largely lives; the
   container-on-Windows step is the one open R&D item in the plan,
   and nothing earlier depends on it.
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
