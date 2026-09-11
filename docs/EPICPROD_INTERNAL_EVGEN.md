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

The steering below is derived from the physics tag and the sample's
own name, with the physics settings of the collaboration's DIS cards
(`eic/eicSimuBeamEffects`, `Pythia8/steerFiles/dis_eicBeam_hiDiv_<E>x<P>_<Q2>`,
one card per beam pair and Q² band, 5x41 to 18x275 with 5x130, 9x130
and 9x275 among them). Those cards made the campaign's older DIS
samples (`pythia8CCDIS_18x275_..._beamEffects_xAngle=-0.025_hiDiv`)
with the beam effects inside Pythia, through eicSimuBeamEffects'
`runBeamShapeHepMC`; the pythia8.316-1.0 samples were generated head-on
with the afterburner applied after, which is this stage's method, and
their card is unrecorded. The comparison below shows the two agree once
the card's physics settings are in: the stage reproduces the registered
pythia8.316-1.0 sample within statistics.

## The stage

`evgen`, between `landing` and `input` in `payload/run.sh`, run only
when the job environment carries `EVGEN_INTERNAL=true`:

1. `payload/evgen_generate.py` composes the generator's steering file
   from the environment (below), one composer per (generator, process)
   pair, and runs the generator under prmon to HepMC3 ASCII: pythia8
   through the driver `payload/evgen_pythia8_hepmc3.cc`, compiled
   against the image's Pythia and HepMC3 (`pythia8-config`,
   `HepMC3-config`; a few seconds, once per job); eSTARlight as the
   image's own `e_starlight`, which reads `slight.in` from its working
   directory and writes `slight.hepmc` beside it.
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
TimeShower:QEDshowerByL = off   (EVGEN_RADIATIVE=off: no QED radiation off the lepton in the shower)
PDF:pset = 2                (CTEQ5L, the collaboration's DIS cards)
PartonLevel:MPI = off
PromptPhoton:all = off
PhaseSpace:mHatMin = 0.0
PhaseSpace:pTHatMinDiverge = force 0.01
SpaceShower:pTmaxMatch = 2
SpaceShower:dipoleRecoil = on
Random:setSeed = on
Random:seed = <run index + 1>
Main:numberOfEvents = <events for the job>
```

The lines from `PDF:pset` to `pTmaxMatch` are the physics settings of
the eicSimuBeamEffects cards (payload 0.13.0, 2026-09-11); before them
the stage ran Pythia's defaults (NNPDF2.3, multiparton interactions on)
and sat a few per cent off the registered sample in x, the cross
section and the multiplicity. The seed is the run index plus one, so a
retried job regenerates the same events, which is what the
delivered-output check (EPICPROD_PAYLOAD.md) assumes of a job's output.
Other generators and processes are added as composers in
`evgen_generate.py`, one per (generator, process) pair, each stated
here.

For `EVGEN_GENERATOR=eSTARlight`, `EVGEN_PROCESS=UPSILON`, exclusive
Upsilon photoproduction e p → e p Υ (the `slight.in` eSTARlight 1.2.0
reads; the image's build is HepMC3-linked, without Pythia or DPMJET):

```
TARGET_BEAM_Z = 1, TARGET_BEAM_A = 1      (ep; other species not composed)
ELECTRON_BEAM_GAMMA = <EBEAM> / m_e
TARGET_BEAM_GAMMA = <PBEAM> / m_p         (eSTARlight puts the electron along −z, the hadron along +z)
PROD_MODE = 12                            (EVGEN_MECHANISM=photo: photon–Pomeron, narrow resonance; threshold production is not composed)
PROD_PID = 553 | 100553 | 200553          (EVGEN_STATE 1s | 2s | 3s)
W_GP_MIN = m_Υ + m_p + 0.1 GeV, W_GP_MAX = 0.99 √s
N_EVENTS = <events for the job>, RND_SEED = <run index + 1>
OUTPUT_FORMAT = 2                         (HepMC3 ASCII)
W_MAX = W_MIN = -1, W_N_BINS = 50, RAP_MAX = 9, RAP_N_BINS = 200, EGA_N_BINS = 400,
CUT_PT = 0, CUT_ETA = 0, BREAKUP_MODE = 5, INTERFERENCE = 0, IF_STRENGTH = 1,
INT_PT_MAX = 0.24, INT_PT_N_BINS = 120, MIN_GAMMA_ENERGY = 6, MAX_GAMMA_ENERGY = 600000,
MIN_GAMMA_Q2 = 0, MAX_GAMMA_Q2 = 100, INT_GAMMA_Q2_BINS = 400   (the generator's example values)
```

The kinematic windows below the seed line are eSTARlight's own example
values, not the exclusive group's, whose steering for the existing
Upsilon samples is unrecorded; they are the knobs to confirm, the Q²
window above all. The Υ decays as eSTARlight decays PID 553 (to μ⁺μ⁻ in
the events read back). Run in the 26.07.1 image at 9×275: total cross
section 10.4 pb, 100 events in 6 s.

For `EVGEN_GENERATOR=DJANGOH`, `EVGEN_PROCESS=DIS_CC`, charged-current
DIS e⁻ p → ν X through W⁻ exchange, HERACLES with LEPTO. DJANGOH is not
in the image; the payload ships the upstream 4.6.21 source
(`payload/djangoh-4.6.21.tgz`, the commit in `djangoh.VERSION`, from
`spiesber/DJANGOH`) with a patch (`djangoh-epicprod.patch`) and builds
it in the job with the image's gfortran and LHAPDF, one legacy flag
(`-fallow-argument-mismatch`) and `-O1`, about 100 s on one core. The
patch does two things upstream does not: the user routine writes every
hadronized event's JETSET record to `djangoh_evt.dat` in the layout
eic-smear reads (the EIC fork's writer, the hadron beam along +z), and
the random seed comes from `DJANGOH_SEED` in the environment when set
(the run index plus one), where upstream seeds from the clock; both
HERACLES and JETSET draw from that one ranlux stream.
`evgen_djangoh_hepmc3.py` then writes HepMC3 ASCII from the event file:
the two beams (JETSET status 21) as HepMC status 4, the final state
(status 1 to 10) as status 1, one vertex, the energy recomputed from
momentum and mass, the sampled cross section (nanobarn in HERACLES) as
the event attribute. The card:

```
EL-BEAM   <EBEAM>D0  -1.0D0  -1      (electron, fully left-handed: the charged current couples to nothing else;
                                       the sampled cross section is twice the unpolarized one)
PR-BEAM   <PBEAM>D0   0.0D0          (proton, unpolarized)
GSW-PARAM 2 1 3 1 1 1 2 1 1 1 1      (EVGEN_RADIATIVE=on: the electroweak corrections; off: 2 0 3 1 0 0 2 1 1 1 1)
KINEM-CUTS 3  0D0 1.00D0  0.01D0 0.95D0  <Q2 lower>D0 <Q2 upper>D0  1.4D0   (x, y, Q², W cuts as the inclusive group's 9x275 CC cards)
INT-OPT-CC 1 20 0 20, SAM-OPT-CC 1 1 0 1   (radiative on; 1 0 0 0 twice when off); the NC options all zero
INT-ONLY 1, INT-POINTS 30000
NUCLEUS <PBEAM>D0 1 1, NUCL-MOD 0
STRUCTFUNC 0 1 9                     (LEPTO's internal parton densities, set 9, as the group's CC cards: the image installs no LHAPDF sets)
POLPDF 0, FLONG 0 0.01 0.03, ALFAS 1 0 0.20 0.235, NFLAVORS 0 5
RNDM-SEEDS -1 -1                     (the seed from DJANGOH_SEED)
START <events for the job>
SOPHIA 1.5, OUT-LEP 1, FRAG 1, CASCADES 1, MAX-VIRT 5
```

Run in the 26.07.1 image at 18×275, Q² 100 to 1000, radiative
corrections on: HERACLES cross section 22.5 pb (for the fully polarized
electron), 100 events in 9 s including the integration, no
hadronization failures. The knobs to confirm with the inclusive group:
the parton densities (their 4.6.21 NC samples used LHAPDF set 10150,
which needs a PDF set the image does not carry), the cuts, and the
electron polarization convention for an unpolarized request.

For `EVGEN_GENERATOR=eicMesonSFGen`, `EVGEN_PROCESS=MESON_SF`, tagged
DIS on the proton's meson cloud: e p → e′ K⁺ Λ for the kaon structure
function (physics tag `channel` k_lambda) and e p → e′ π⁺ n for the
pion one (pi_n). The generator (JeffersonLab eic_mesonsf_generator
1.0.0, `payload/mesonsf-1.0.0.tgz`, the commit in `mesonsf.VERSION`) is
C++ compiled in the job by ROOT's ACLiC with its bundled CTEQ6 tables;
the patch `mesonsf-epicprod.patch` writes its outputs into the working
directory instead of the author's JLab paths and gives a value to a
function that returned none (ROOT's compiler refuses it). Its steering
is the argument list of `mainx(xMin, xMax, Q2Min, Q2Max, seed, trials,
pBeam, kBeam, smear)`: the x window from the physics tag's `x_range`
(`x_0.001to1` by default), the Q² window from `q2_range`, the beams,
the run seed, one trial per requested event (this version yields an
event per trial, and the file is trimmed to the requested count), no
beam smearing, and the generator's zero crossing angle, which its own
header sets for the afterburner's sake. It writes HepMC3 ASCII itself,
the two beams as status 4 (electron along −z, proton along +z) and e′,
the baryon and the meson as status 1. Run in the 26.07.1 image at
10×100, k_lambda: 100 events in 34 s including the compilation. Two
things for the exclusive group: the k_sigma and pi_p scripts keep
further hard-coded output paths and are not composed; and in the events
read back the kaon carries the scattered electron's transverse momentum
with the same sign, so transverse momentum is not conserved event by
event, which is the generator's kinematics as released (the catalog's
MESON_SF samples come from the same version).

### Comparison with the registered sample

`tools/evgen/compare-pythia8.py`, run in the campaign image: the stage's
composer and driver generate N events (before the afterburner), the
first N events of the registered sample are read from the JLab door,
and the quantities below are computed the same way on both, from
Lorentz invariants of the beams and the scattered electron (the
highest-energy final-state electron), so the registered sample's
crossing angle and beam spreads do not enter. 10,000 events of 10×100
NC `q2_10to100` in the 26.07.1 image against
`pythia8.316-1.0_NC_noRad_ep_10x100_q2_10to100_run000` (2026-09-11,
payload 0.13.0):

| Quantity | Stage | Registered |
|---|---|---|
| Hard process | Pythia 211 (NC γ*/Z exchange), 10,000 of 10,000 | 211, 10,000 of 10,000 |
| Q, mean / median | 4.56 / 4.06 | 4.57 / 4.07 |
| x, mean | 0.0986 | 0.0987 |
| Cross section (pb) | 3.87 × 10⁴ | 3.83 × 10⁴ |
| Final-state multiplicity, mean | 18.32 | 18.42 |
| Final-state photons per event | 7.98 | 8.09 |

Every quantity agrees within its statistical error (one per cent on
the cross section at 10,000 events). Before the card's physics settings
(3,000 events, 2026-09-07 and 2026-09-11) the stage sat 2 per cent low
in cross section, 10 per cent low in mean x and high in multiplicity
and photons; the settings that closed it are CTEQ5L, multiparton
interactions off and lepton QED showering off. The earlier reading
that the registered sample's multiplicity implied lepton QED showering
on was wrong: the excess came from multiparton interactions. The beam
spreads in the registered sample identify the afterburner setting as
high divergence, so that is the stage's default.

A generator-level shape check on a trial's generated sample, Rivet's
`MC_DIS` analysis as in the detector_benchmarks pythia8 proof of
concept (eic/detector_benchmarks PR 342), is the validation to add when
a reference to compare against exists (the collaboration's generator
benchmarks, proposed 2026-09-11).

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
| `EVGEN_PROCESS`, `EVGEN_BEAM_SPECIES` | physics tag |
| `EVGEN_Q2_RANGE` | physics tag `q2_range`; required for a DIS process, passed through otherwise when present |
| `EVGEN_STATE`, `EVGEN_MECHANISM`, `EVGEN_CHANNEL`, `EVGEN_X_RANGE`, `EVGEN_BEAM_CONFIG` | physics tag `state`, `mechanism`, `channel`, `x_range`, `beam_config`, those present: an exclusive process is steered by these, with or without a Q² range |
| `EBEAM`, `PBEAM` | physics tag (already carried) |
| `DETECTOR_BEAMS` | production config `data['detector_beams']`, absent by default: the beams whose detector geometry the job simulates with when the image has no compact file for the physics beams (payload stage `geometry`, exit 83; EPICPROD_PAYLOAD.md) |
| `EVGEN_AB_PRESET` | production config `data['afterburner_preset']`, default 0 (IP6 high divergence); a number selects a configuration of the image's afterburner, a name builds the shipped source (The stage, above) |
| `COPYEVGEN` | production config `copy_evgen`, default false; a trial sets true |

The sample path and name follow the production team's EVGEN layout. DIS:
`DIS/<generator>.<version>/<NC or CC>/<noRad or Rad>/<species>/<ExP>/q2_<bin>`.
Any other process takes the exclusive samples' shape, the physics tag's
category as the top directory and the tag's state, mechanism, channel
and beam_config (those present, in that order) as the qualifier:
`EXCLUSIVE/UPSILON/eSTARlight1.2.0/ep/9x275/1s_photo_hiAcc`, the file
`eSTARlight1.2.0_UPSILON_1s_photo_hiAcc_ep_9x275_run<NNN>.hepmc3.tree.root`
(`pcs/commands.py internal_evgen_sample`).

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

The 5×130 request is composed the same way. Two of its knobs have no
right value in the 26.07.1 image, and its trials declare stand-ins for
both on the task's configuration (`overrides.data`): no afterburner
version has a 130×5 beam configuration and the images carry 0.1.3,
which has no 130 GeV configuration at all, so `afterburner_preset` is
`ip6_ep_130x9`, built in the job from the shipped source (payload
0.7.1), the 10×100 beam parameters relabelled ("no official numbers" in
the afterburner source); and the image has no detector compact file
for 5×130, so `detector_beams` is `5x100`, the electron-side optics
right and the hadron side 30 per cent off (10×130 would have been the
reverse; for an inclusive DIS trial the electron side is the one that
matters). The beam effects and the far-forward geometry of that sample
are known to be wrong; it is a trial of the path, not a physics sample.

The first trial, JEDI task 39362 (job 2721364, BNL_OSG_PanDA_1, payload
0.7.1, 2026-09-07), generated its 100 events and built and ran the
afterburner in the job, then failed at simulation: npsim exits 1 when
the compact file for the beams does not exist, and nothing had checked
before the generation ran. Payload 0.8.0 adds the `geometry` stage and
the `detector_beams` stand-in (EPICPROD_PAYLOAD.md, exit 83); the second
trial, `group.EIC.26.07.1.epic_craterlake.p2445.e49.s1.r1.trial2`,
carries both stand-ins. The asks that retire them: a 130×5
configuration in the afterburner and an afterburner in the campaign
image that carries it and the 9 GeV configurations; a 5×130 detector
geometry. The steering card question is closed by the comparison
above.

The second trial, JEDI task 39366 (job 2721378, BNL_OSG_PanDA_1, payload
0.8.0, 2026-09-07), passed: every stage ok, the geometry stage recording
`epic_craterlake_5x100.xml, a stand-in for beams 5x130`, the generation
stage 50 s wall for 100 events including the afterburner build,
simulation 728 s, reconstruction 113 s, 100 events simulated and
reconstructed, the log uploaded to EIC-XRD-LOG, and the generated sample
and the RECO output registered with their event counts under
`epic:/TEST/trial/group.EIC.26.07.1.epic_craterlake.p2445.e49.s1.r1.trial2/`.
Pilot to finish, 17 minutes. The first request on the priority list
produced end to end, both stand-ins on record.

### Upsilon photoproduction (eSTARlight)

The exclusive group's Upsilon request (1S, 2S, 3S at 9×275 and 9×130)
is composed as six configurations on the same Internal EVGEN production
config: physics tags with `process` UPSILON, `state`, `mechanism` photo
and, at 9×275, `beam_config` hiAcc (p5446 to p5451); the EvGen tag
eSTARlight 1.2.0 (e50); afterburner stand-ins `ip6_hiacc_275x9` and
`ip6_ep_130x9` from the shipped source. The 9×130 tags carry no
`beam_config`: the afterburner has one 130×9 configuration, itself a
stand-in. The image has geometries for 9×275 and 9×130. One
representative trial, Υ(1S) photoproduction at 9×275
(`group.EIC.26.07.1.epic_craterlake.p5446.e50.s1.r1.trial`), proves the
path for all six; the others are records until their shapes prove to
differ. Proven locally in the image before submission: 100 events
generated in 6 s, the afterburner built in 33 s and applied at 275×9,
the sample at
`EVGEN/EXCLUSIVE/UPSILON/eSTARlight1.2.0/ep/9x275/1s_photo_hiAcc/`.

The trial ran as JEDI task 39365 (job 2721377, BNL_OSG_PanDA_1, payload
0.8.0, 2026-09-07) and passed: every stage ok, the geometry stage
resolving `epic_craterlake_9x275.xml`, the generation stage 50 s wall
for 100 events including the afterburner build, simulation 410 s,
reconstruction 77 s, 100 events simulated and reconstructed, the log
uploaded to EIC-XRD-LOG, and the generated sample and the RECO output
registered with their event counts under
`epic:/TEST/trial/group.EIC.26.07.1.epic_craterlake.p5446.e50.s1.r1.trial/`
in the exclusive layout. Pilot to finish, 11 minutes. It is the first
configuration produced end to end with a generator other than pythia8,
and the first exclusive one.

### DJANGOH charged current

The DJANGOH CC request (18×275 and 9×275) is composed as two
configurations on the Internal EVGEN production config: physics tags
DIS_CC ep q2_100to1000 at 18×275 (p2452) and 9×275 (p2453), the EvGen
tag DJANGOH 4.6.21 with radiative corrections on (e51); the 9×275 task
carries the afterburner stand-in `ip6_hidiv_275x9`, the 18×275 one runs
the image's afterburner. One representative trial, 18×275
(`group.EIC.26.07.1.epic_craterlake.p2452.e51.s1.r1.trial`). The 9×275
configuration stands as a record: the inclusive group has already
produced polarized 9×275 CC inputs with DJANGOH 4.6.10
(`eic/djangoh_production`, release DJANGOH4.6.10-2.0, 2026-09-04: six
samples, proton helicity plus and minus, Q² 100 to 9000, HERACLES,
eic-smear, the afterburner at `ip6_hidiv_275x9`, npsim-validated) in a
JLab work area. Registered under `/EVGEN/DIS/CC/…` in JLab Rucio they
are external inputs to the ordinary path, which is the ask to the
group. That release carries no 18×275.

The trial ran as JEDI task 39370 (job 2721385, BNL_OSG_PanDA_1, payload
0.9.0, 2026-09-07) and passed: every stage ok, the geometry stage
resolving `epic_craterlake_18x275.xml`, the generation stage 67 s wall
for 100 events including the DJANGOH build and the conversion,
simulation 1627 s (charged-current events at 18×275 carry far more
energy into the detector than the 10×100 neutral-current ones, whose
simulation took 640 s), reconstruction 122 s, 100 events simulated and
reconstructed, the log uploaded to EIC-XRD-LOG, and the generated sample
and the RECO output registered with their event counts under
`epic:/TEST/trial/group.EIC.26.07.1.epic_craterlake.p2452.e51.s1.r1.trial/`
in the DIS layout (`DIS/DJANGOH4.6.21/CC/Rad/ep/18x275/q2_100to1000`).
Pilot to finish, 33 minutes. The first configuration produced end to
end with a generator built from Fortran source in the job.

### Kaon structure function (eicMesonSFGen)

Composed as one configuration on the Internal EVGEN production config:
physics tag MESON_SF ep 10×100, channel k_lambda, x 0.001 to 1, Q² 1
to 1000 (p5454), the existing EvGen tag eicMesonSFGen 1.0.0 (e25), the
image's afterburner. One trial
(`group.EIC.26.07.1.epic_craterlake.p5454.e25.s1.r1.trial`) on the
current release; the reconstruction fix the request names rides
whichever campaign release carries it, and the configuration reruns
there unchanged. Proven locally in the image before submission: 100
events in 41 s including the compilation, the afterburner applied, the
sample at `EVGEN/EXCLUSIVE/MESON_SF/eicMesonSFGen1.0.0/ep/10x100/k_lambda/`.

The trial ran as JEDI task 39371 (job 2721386, BNL_OSG_PanDA_1, payload
0.10.0, 2026-09-07) and passed: every stage ok, the geometry stage
resolving `epic_craterlake_10x100.xml`, the generation stage 317 s wall
for 100 events at 5 per cent CPU efficiency (ROOT's compilation of the
generator reading its headers from cvmfs on that worker; 34 s here),
simulation 661 s, reconstruction 84 s, 100 events simulated and
reconstructed, the log uploaded to EIC-XRD-LOG, and the generated
sample and the RECO output registered with their event counts under
`epic:/TEST/trial/group.EIC.26.07.1.epic_craterlake.p5454.e25.s1.r1.trial/`
in the exclusive layout. Pilot to finish, 22 minutes.

### BeAGLE eAg 9×115

Composed as a record only: physics tag DIS_NC eAg (Ag107) 9×115
q2_1to10 (p2455), the existing EvGen tag BeAGLE 1.03.02-1.0 (e21),
dataset and task with the stand-ins the trial would have declared
(afterburner `ip6_eau_110x10`, no afterburner version having a silver or
a 115 GeV configuration; geometry `9x115_Cu63`, the image having no
silver compact file). No trial: BeAGLE has no build route into the
campaign image, since it links FLUKA, whose library is licensed and not
ours to ship, so the only built BeAGLE is the legacy BNL tree on cvmfs
(`/cvmfs/eic.opensciencegrid.org/x8664_sl7/MCEG/releases`, SL7). That
binary runs in the campaign image through FLUKA's initialization once
`nuclear.bin` is in the working directory and the SL7 `libgfortran.so.3`
from the tree's own container image is on its library path, and then
dies with a segmentation fault in PYTHIA 6's `PDFSET` (LHAPDF 5, set
10042), at every release level of the tree and inside the tree's own
SL7 container as well (2026-09-07). The stop is the legacy LHAPDF 5
installation, not the image. What moves it: the group generating eAg
9×115 with a working BeAGLE setup, or BeAGLE built in the image against
a FLUKA the collaboration may ship.

## Related

- [EPICPROD_PAYLOAD.md](EPICPROD_PAYLOAD.md) — the payload this stage
  joins; evolution item 7.
- [PCS_DATASET_REQUEST_WORKFLOW.md](PCS_DATASET_REQUEST_WORKFLOW.md) —
  the workflow modes and the dataset model.
- [JEDI_INTEGRATION.md](JEDI_INTEGRATION.md) — the `noInput`
  submission and the manifest convention.
- [EPICPROD_EVGEN_INPUTS.md](EPICPROD_EVGEN_INPUTS.md) — externally
  supplied EVGEN, the mode this complements.
