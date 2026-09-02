# Rucio registration contract

What every Rucio registration performed by epicprod scripts and
processes must carry. A registration that omits an item below is
incomplete, whatever else succeeded.

## 1. Event count on every file

Every file DID registered by an epicprod process carries Rucio's
`events` attribute, set at registration time from the file itself.
Rucio derives a dataset's `events` from its files and refuses a direct
write on the dataset, so the file value is the only way a dataset ever
acquires a count; a registration that sets bytes and checksum alone
leaves the dataset's count empty forever. Checked on 2026-09-02: no
EVGEN or produced RECO dataset in the `epic` scope carried a count,
because no registration path had ever written one to a file.

Sources of the count:

| Data | Count from | Cost |
|---|---|---|
| EVGEN inputs (HepMC3 ROOT) | the `hepmc3_tree` entry count in the file's tree header | one open per file through the door, about a second; no event bytes move |
| Produced FULL and RECO (podio ROOT) | the `events` tree entry count in the output file, known to the job that wrote it | local read in the job before upload |

Verification is part of the contract: after the file writes, the
dataset's derived `events` is read back and held equal to the sum of
its files. A file whose count cannot be read is reported by name and
leaves its dataset unverified; a partial total is never presented as
the whole.

Status by registration path:

| Path | Registers | Count written | State |
|---|---|---|---|
| EVGEN registration doer (`register-evgen-rucio.py`, the "Register in Rucio" action) | EVGEN input files and datasets in JLab Rucio | yes, as the registration's second step; verified against the derived dataset total | in place 2026-09-02 (EPICPROD_EVGEN_INPUTS.md § Registration) |
| EVGEN datasets registered before 2026-09-02 | — | no | backfill pending: the same doer mode over every registered EVGEN dataset without a count |
| Produced FULL and RECO, registered by the campaign payload (`simulation_campaign_hepmc3/scripts/register_to_rucio.py` via `run.sh`, under the epicprod runner) | output files and datasets in JLab Rucio, with dataset tag metadata (software release, geometry, data level, beam parameters) | no | open: the per-file `events` write belongs in the job's registration step, where the count is known; to be integrated with the run-script changes planned in RUCIO_RESILIENCE.md |

## Enforcement

The EVGEN inventory assimilation records each dataset's `events` from
Rucio and the EVGEN inputs page shows it, so a registered dataset
without a count is visible as such. The same visibility for produced
data follows when its registration path writes the count.
