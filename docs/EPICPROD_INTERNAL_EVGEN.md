# Internal EVGEN: event generation as a payload stage

A production task whose events are generated in the job rather than
read from a sample someone else produced. It is the `internal_evgen`
workflow mode PCS_DATASET_REQUEST_WORKFLOW.md § Workflow modes
anticipates and `ProdConfig.workflow_mode` declares: the same PCS task,
the same submission path and the same payload as an external-input
task, with one stage added ahead of the input stage. Nothing downstream
of that stage changes.

## Why now

Of the seven requests on the physics groups' priority list of
2026-09-06, six need event generation before anything can be produced;
the registered EVGEN catalog holds none of them. One is a beam-energy
variant of a configuration the catalog holds at eight other energies:
inclusive neutral-current DIS, pythia8 8.316-1.0, ep 5×130, without
radiative corrections. Its generator is in the campaign image, its
process and beam treatment are readable from the existing samples, and
every other request adds a generator or a species the production
system has never run. It is the first configuration produced end to
end from a request, and it breaks the dependence on externally supplied
input for every configuration whose generator the image carries.

## What the existing samples are

Read from a registered file of the 9×130 sample
(`/EVGEN/DIS/pythia8.316-1.0/NC/noRad/ep/9x130/q2_10to100`): Pythia
process 211, `f f' → f f'` via γ*/Z⁰ exchange, the neutral-current DIS
process; the hadron beam along +z and the electron along −z; and the
afterburner's run attributes — crossing angle −0.025 rad, vertical
0.0001 rad, divergence/acceptance setting 1, and the two beam energies
— with the hadron beam rotated by the crossing angle. The file carries
no steering card. The campaign image `eic_xl:26.07.1-stable` holds
Pythia 8.316 with its HepMC3 interface, HepMC3 3.3.0, `abconv` (the
afterburner) and g++; Pythia's Python binding is not built.

The steering below is therefore derived from the physics tag and the
sample's own name, and stated as the production system's. The inclusive
group is asked to confirm it against the card used for the existing
samples; the trial does not wait for that.

## The stage

`evgen`, between `landing` and `input` in `payload/run.sh`, run only
when the job environment carries `EVGEN_INTERNAL=true`:

1. `payload/evgen_generate.py` composes the Pythia command file from
   the environment (below), compiles the driver
   `payload/evgen_pythia8_hepmc3.cc` against the image's Pythia and
   HepMC3 (`pythia8-config`, `HepMC3-config`; a few seconds, once per
   job), and runs it under prmon: the requested number of events to
   HepMC3 ASCII.
2. `abconv` applies the beam effects (the IP6 preset the environment
   names, energies read from the file) and writes `hepmc3.tree.root`.
   The image's afterburner (0.1.3 in the 26.07 images) carries beam
   parameters for ep 18×275, 10×275, 10×100, 5×100 and 5×41, with
   approximate 10×250 and 10×130, selected by number; the 9 GeV-electron
   configurations are in the afterburner repository (main after v0.2.1)
   but not in the images, and no version has 5×130 (Trials, below). So
   the payload ships the repository's C++ source
   (`payload/afterburner-cpp.tgz`; the commit it was taken from is in
   `afterburner-cpp.VERSION`) and, when the preset is a name such as
   `ip6_ep_130x9` rather than a number, or `EVGEN_AB_BUILD=true`, builds
   `abconv` from it in the job (cmake, one core, 30 to 40 s in the
   26.07.1 image) and runs that build; a numbered preset runs the
   image's own `abconv`. The stage summary records which afterburner
   ran and the build time.
3. The file is placed at the path and name an externally supplied
   sample would have had:
   `EVGEN/<process path>/<generator>_<process>_<rad>_<species>_<beams>_<q2 bin>_run<NNN>.hepmc3.tree.root`.
   The input stage then takes the local file where it would have
   streamed one from the JLab door, and background merging, simulation,
   reconstruction, metadata, naming, validation and registration run
   as they do for an external input.

The stage records its outcome and the events generated in the stage
log and the payload report like every other stage. The generated file
is an output when `COPYEVGEN=true`: validated for an `events` tree,
uploaded and registered under `EVGEN/` in the output layout with the
same metadata and lifetime treatment as FULL and RECO, so a
PCS-produced generator sample is an ordinary dataset with
`stage=evgen` (PCS_DATASET_REQUEST_WORKFLOW.md § Model Direction). A
trial sets it; production decides per configuration.

## The steering

For `EVGEN_GENERATOR=pythia8`, `EVGEN_PROCESS=DIS_NC`:

```
Beams:frameType = 2
Beams:idA = 2212            (hadron along +z; species from EVGEN_BEAM_SPECIES)
Beams:eA = <PBEAM>
Beams:idB = 11
Beams:eB = <EBEAM>
WeakBosonExchange:ff2ff(t:gmZ) = on
PhaseSpace:Q2Min = <lower bound of EVGEN_Q2_RANGE>
PhaseSpace:Q2Max = <upper bound, omitted for INF>
PDF:lepton = off            (EVGEN_RADIATIVE=off: no initial-state QED radiation from the lepton)
SpaceShower:dipoleRecoil = on
Random:setSeed = on
Random:seed = <run index + 1>
Main:numberOfEvents = <events for the job>
```

Final-state QED showering stays at Pythia's default. The seed is the
run index plus one, so a retried job regenerates the same events, which
is what the delivered-output check (EPICPROD_PAYLOAD.md) assumes of a
job's output. Other generators and processes are added as composers in
`evgen_generate.py`, one per (generator, process) pair, each stated
here.

### Comparison with the registered sample

3000 events of 10×100 NC `q2_10to100` generated by the stage in the
26.07.1 image against the first 3000 of the registered
`pythia8.316-1.0_NC_noRad_ep_10x100_q2_10to100_run000` (2026-09-07):

| Quantity | Stage | Registered |
|---|---|---|
| Hard process | Pythia 211 (NC γ*/Z exchange) | 211 |
| Proton px at 100 GeV (crossing angle) | −2.500 ± 0.019 | −2.499 ± 0.023 |
| Electron beam pT (divergence), mean | 1.30 MeV (high acceptance) / matches at high divergence | 1.55 MeV |
| Q, mean / median | 4.01 / 3.67 | 3.97 / 3.63 |
| Cross section (pb) | 3.75 ± 0.04 × 10⁴ | 3.86 ± 0.04 × 10⁴ |
| Final-state multiplicity, mean | 18.0 with lepton QED showering off; 18.6 with it on | 18.6 |
| Final-state photons per event | 8.35 | 8.13 |
| x, mean | 0.096 | 0.102 |

The beam spreads identify the registered sample's afterburner setting
as high divergence, so that is the stage's default. The multiplicity
identifies lepton QED showering as on. The remaining differences, in x
and the cross section at the few-per-cent level, are a PDF, tune or
phase-space choice the files do not record; the card used for the
registered samples settles them and is the standing ask to the
inclusive group.

## PCS

The configuration is composed as any other: a physics tag (`DIS_NC`,
`ep`, `q2_10to100`, electron 5, hadron 130), an EvGen tag naming the
generator and version with `radiative=off` (the physics-configuration
identity reads generator, version and radiation from it,
`pcs/physics_config.py`), the campaign's simulation and reconstruction
tags, and a production config cloned from the campaign's standard one
with `workflow_mode=internal_evgen` and `submission_path=panda`. The
dataset's `metadata` records `stage=reco` as usual; the task has no
input dataset.

The submission spec (`pcs/commands.py build_evgen_task_params`) reads
the mode from the config. In internal mode it synthesizes the manifest
instead of resolving matched input files: one row per job,
`<process path>/<sample>_run<NNN>,hepmc3.tree.root,<events_per_job>,0000`,
with `n_jobs` rows from the config (one for a trial), and adds the
generation environment to the payload environment:

| Variable | From |
|---|---|
| `EVGEN_INTERNAL` | `true` |
| `EVGEN_GENERATOR`, `EVGEN_GENERATOR_VERSION` | EvGen tag |
| `EVGEN_RADIATIVE` | EvGen tag `radiative` |
| `EVGEN_PROCESS`, `EVGEN_Q2_RANGE`, `EVGEN_BEAM_SPECIES` | physics tag |
| `EBEAM`, `PBEAM` | physics tag (already carried) |
| `EVGEN_AB_PRESET` | production config `data['afterburner_preset']`, default 0 (IP6 high divergence); a number selects a configuration of the image's afterburner, a name builds the shipped source (The stage, above) |
| `COPYEVGEN` | production config `copy_evgen`, default false; a trial sets true |

The sample path and name follow the production team's EVGEN layout:
`DIS/<generator>.<version>/<NC or CC>/<noRad or rad>/<species>/<ExP>/q2_<bin>`.

## Trials

The first trial regenerates a configuration the catalog already holds,
10×100 NC `q2_10to100`, so the stage is proved against a registered
sample rather than trusted:
`group.EIC.26.07.1.epic_craterlake.p2343.e<n>.s1.r1.trial`, one job,
100 events, outputs under `epic:/TEST/trial/<name>/` in the production
layout (EVGEN, FULL, RECO, LOG), two-week lifetime. Acceptance is the
ordinary one — the payload report's stages all ok, the reconstructed
event count equal to the request, the registrations confirmed in the
catalog — plus the comparison above on the generated sample.

That trial ran as JEDI task 39358 (job 2721360, BNL_OSG_PanDA_1,
2026-09-07, payload 0.7.0) and passed: every stage ok, the generation
stage 40 s wall for 100 events including the driver build, simulation
639 s, reconstruction 107 s, 100 events simulated and reconstructed,
the log uploaded to EIC-XRD-LOG, and the generated sample and the RECO
output registered in the catalog with their event counts, under the
trial root. Pilot to finish, 14 minutes.

The 5×130 request is composed the same way
(`group.EIC.26.07.1.epic_craterlake.p2445.e49.s1.r1.trial`). No version
of the afterburner has a 130×5 beam configuration, and the 26.07 images
carry 0.1.3, which has no 130 GeV configuration at all; so its trial
runs the afterburner built in the job from the shipped source (payload
0.7.1) with the stand-in configuration `ip6_ep_130x9`, the 10×100 beam
parameters relabelled ("no official numbers" in the afterburner
source), set on the task and its trial through
`overrides.data.afterburner_preset`. The beam effects in that sample
are known to be wrong; it is a trial of the path, not a physics
sample. The asks that retire the stand-in: a 130×5 configuration in
the afterburner, and an afterburner in the campaign image that carries
it and the 9 GeV configurations. The steering card used for the
registered pythia8 samples is the third ask, for every pythia8
request.

## Related

- [EPICPROD_PAYLOAD.md](EPICPROD_PAYLOAD.md) — the payload this stage
  joins; evolution item 7.
- [PCS_DATASET_REQUEST_WORKFLOW.md](PCS_DATASET_REQUEST_WORKFLOW.md) —
  the workflow modes and the dataset model.
- [JEDI_INTEGRATION.md](JEDI_INTEGRATION.md) — the `noInput`
  submission and the manifest convention.
- [EPICPROD_EVGEN_INPUTS.md](EPICPROD_EVGEN_INPUTS.md) — externally
  supplied EVGEN, the mode this complements.
