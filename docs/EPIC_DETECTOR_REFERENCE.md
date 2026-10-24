# ePIC Detector Reference

When a detector is behind a production problem, reconstruction blowing up
in one subsystem, a crash signature naming a geometry volume, a finding on
one part of the computing-centre model, the place to learn about it is the
ePIC Preliminary Design Report. This document is the way into the report,
not a copy of it: where the report lives, which detector names lead to
which of its sections, and a small tool that finds a name or a term in it
(Torre Wenaus, 2026-10-20: "a focused targeted ref center in the repo";
"you can build light nav tools into the doc").

## The report

- **ePIC Preliminary Design Report, version 3.1**, ePIC Collaboration,
  January 2026, CC BY 4.0, doi 10.5281/zenodo.18271602. 553 PDF pages;
  the printed page numbers below are the report's own (chapter text from
  1; front matter in roman numerals).
- **On swf-testbed:** `/data/wenauseic/epic-pdr/`, the PDF
  (`ePIC_Preliminary_Design_Report_3.1.pdf`) and the LaTeX source
  (`source/`, the Overleaf project, built from `source/PDR.tex`).
- **What is where:** Chapter 2 sets each system's requirements; Chapter 3
  section 2, *The ePIC Detector*, describes every system, each in
  Requirements, Justification and Implementation; Chapter 3 also covers
  detector integration and commissioning; Chapter 4 is physics
  performance, including the beam-background studies.

## Detector names to report sections

The names are the top-level detectors of the 26.07.3 `epic_craterlake`
geometry, all 92 of them, as DD4hep, findings, crash signatures and the
room's detector model use them. Pages are printed pages; the tool gives
the PDF pages too.

| System | DD4hep detectors | PDR section | Pages | Requirements |
|---|---|---|---|---|
| Magnet | `SolenoidBarrel`, `SolenoidEndcapP`, `SolenoidEndcapN` | Magnet | 25–62 | - |
| Silicon trackers (SVT) | `VertexBarrelSubAssembly`, `InnerSiTrackerSubAssembly`, `MiddleSiBarrelSubAssembly`, `OuterSiBarrelSubAssembly`, `MiddleSiEndcapSubAssembly`, `OuterSiEndcapSubAssembly`, `VertexBarrel`, `SVT_IB_Support_L2`, `SVT_IB_Support_L1`, `SVT_IB_Support_L0_L1`, `SagittaSiBarrel`, `OuterSiBarrel`, `InnerTrackerSupport`, `InnerTrackerEndcapP`, `InnerTrackerEndcapN`, `MiddleTrackerEndcapP`, `MiddleTrackerEndcapN`, `OuterTrackerEndcapP`, `OuterTrackerEndcapN` | The silicon trackers | 65–86 | Requirements of Tracking and Momentum Measurement in the Central Detector, p. 7 |
| MPGD trackers | `EndcapMPGDSubAssembly`, `InnerMPGDBarrelSubAssembly`, `OuterBarrelMPGDSubAssembly`, `InnerMPGDBarrel`, `MPGDOuterBarrel`, `ForwardMPGD`, `BackwardMPGD` | The MPGD trackers | 86–102 | Requirements of Tracking and Momentum Measurement in the Central Detector, p. 7 |
| Time of flight (TOF) | `EndcapTOFSubAssembly`, `BarrelTOFSubAssembly`, `BarrelTOF`, `BackwardTOF`, `ForwardTOF` | The time-of-flight layers | 105–126 | Particle Identification (PID), p. 9 |
| pfRICH | `PFRICH` | The proximity focusing RICH | 126–140 | Particle Identification (PID), p. 9 |
| hpDIRC | `cb_DIRC` | The high performance DIRC | 140–154 | Particle Identification (PID), p. 9 |
| dRICH | `DRICH` | The dual radiator RICH | 154–172 | Particle Identification (PID), p. 9 |
| Backward EMCal (EEEMCal) | `EcalEndcapN` | The backward endcap electromagnetic calorimeter | 173–184 | Electromagnetic Calorimetry, p. 8 |
| Barrel EMCal (BIC) | `EcalBarrelTrackerSubAssembly`, `EcalBarrelImaging`, `EcalBarrelScFi` | The barrel electromagnetic calorimeter | 184–205 | Electromagnetic Calorimetry, p. 8 |
| Forward EMCal (pEMCal) | `EcalEndcapP` | The forward endcap electromagnetic calorimeter | 205–217 | Electromagnetic Calorimetry, p. 8 |
| Backward HCal (nHCal) | `HcalEndcapN`, `FluxEndcapN` | The backward endcap hadronic calorimeter | 217–234 | Hadronic Calorimetry, p. 9 |
| Barrel HCal (BHCal) | `HcalBarrel`, `FluxBarrel` | The barrel hadronic calorimeter | 234–253 | Hadronic Calorimetry, p. 9 |
| Forward HCal (LFHCAL and insert) | `LFHCAL`, `HcalEndcapPInsert`, `FluxEndcapP` | The forward endcap hadronic calorimeter | 253–267 | Hadronic Calorimetry, p. 9 |
| Roman pots and off-momentum detectors | `ForwardOffMTracker_station_1`, `ForwardOffMTracker_station_2`, `ForwardOffMTracker_station_3`, `ForwardOffMTracker_station_4`, `ForwardRomanPot_Station_1`, `ForwardRomanPot_Station_2` | The roman pots and the off-momentum detectors | 268–277 | Far-Forward and Far-Backward Detectors, p. 10 |
| B0 detectors | `B0TrackerSubAssembly`, `B0TrackerCompanion`, `B0Tracker`, `B0ECal` | B0 detectors in the B0 magnet | 277–286 | Far-Forward and Far-Backward Detectors, p. 10 |
| Zero Degree Calorimeter (ZDC) | `HcalFarForwardZDC_SiPMonTile`, `ZDC_Crystal` | The Zero Degree Calorimeter | 286–300 | Far-Forward and Far-Backward Detectors, p. 10 |
| Luminosity system | `LumiWindow`, `LumiCollimator`, `SweeperMag`, `AnalyzerMag`, `LumiSpecTracker`, `LumiSpecCAL`, `LumiPhotonChamber`, `LumiDirectPCAL` | The luminosity system | 300–321 | Far-Forward and Far-Backward Detectors, p. 10 |
| Low-Q2 taggers | `BackwardsTaggerVacuum`, `BackwardsTaggerStation` | The low-Q2 taggers | 321–340 | Far-Forward and Far-Backward Detectors, p. 10 |
| Beam pipe, beamline magnets and vacuum | `BeamPipe`, `B0PF_BeamlineMagnet`, `B0APF_BeamlineMagnet`, `Q1APF_BeamlineMagnet`, `Q1BPF_BeamlineMagnet`, `Q2PF_BeamlineMagnet`, `B1PF_BeamlineMagnet`, `B1APF_BeamlineMagnet`, `BeamPipeB0`, `B0Window`, `Pipe_cen_to_pos`, `Q0EF_BeamlineMagnet`, `Q1EF_BeamlineMagnet`, `VacuumMagnetElement`, `Q1ER_BeamlineMagnet`, `Q2ER_BeamlineMagnet`, `Magnets_Q3eR`, `Q1APR_BeamlineMagnet`, `Q1BPR_BeamlineMagnet`, `Q2PR_BeamlineMagnet`, `Pipe_Q1eR_to_B2BeR` | Detector Integration | 411–431 | - |

The beam pipe, the beamline magnets and the vacuum volumes have no section
of their own; Detector Integration covers their installation, and
*Backgrounds and Rates* (p. 18) and the tracker and calorimeter
justifications discuss them where they matter. For them, search the text.

Topics that belong to no single detector, with their printed pages:
Backgrounds and Rates (p. 18), Radiation Level (p. 21), Readout
Electronics and Data Acquisition (p. 369), Software and Computing
(p. 397), Detector Integration (p. 411), Incorporation of Beam Backgrounds
in the Studies (p. 450), Polarimeters (p. 340).

## The tool

```bash
python3 scripts/pdr-lookup.py DRICH                 # a detector's section, pages, source and parts
python3 scripts/pdr-lookup.py ForwardRomanPot       # a name prefix: every matching detector
python3 scripts/pdr-lookup.py --list                # every system with its detectors
python3 scripts/pdr-lookup.py --grep 'aerogel'      # a term in the source, with its heading and page
python3 scripts/pdr-lookup.py DRICH --text Requirements   # one part's text, from the PDF
```

A detector name is matched case-insensitively, whole or as a prefix; a
system name or a word of a section title works too. `--text` needs
`pdftotext`. The tool reads only `docs/epic-detector-reference.json`.

## The index

`docs/epic-detector-reference.json`, built by `scripts/pdr-index.py` from
three sources: the PDF's outline (237 headings down to the Requirements,
Justification and Implementation level, each with its printed and PDF
page and the page where it ends), the LaTeX source (the `.tex` file and
line behind each heading, 231 of the 237 matched, and the files each
heading's text lives in), and the 26.07.3 geometry in the campaign image
(each detector's DD4hep type and compact file). The mapping from detector
names to systems is the `SYSTEMS` table in the script, written by hand
from the geometry and the report's headings; the build fails loudly on a
detector no system claims. Rebuild when the report or the geometry release
changes:

```bash
python3 scripts/pdr-index.py              # needs pypdf
python3 scripts/pdr-index.py --markdown   # the table above
```
