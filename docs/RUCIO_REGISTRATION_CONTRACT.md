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
| Produced FULL and RECO, registered by the epicprod payload (`swf_epicprod/payload/register_to_rucio.py --events` via `run.sh`) | output files and datasets in JLab Rucio, with dataset tag metadata (software release, geometry, data level, beam parameters) | yes, from the output file's `events` tree entry count, read in the job after simulation and reconstruction; the dataset's derived total read back and held to the sum of its files | in place 2026-09-06, payload 0.2.0 (EPICPROD_PAYLOAD.md, evolution item 1) |
| Produced FULL and RECO registered by the campaign payload before 2026-09-06, and by the condor submission path | — | no, and none is written: the counts live in the delivery events store, not in Rucio (ruling 2026-09-13) | 26.07 RECO: exact planned counts from the PanDA task sandboxes for the tasks whose sandbox survived (provenance `sandbox`, CAMPAIGN_DELIVERY.md § The events source); the rest carry the store's inferred counts. No door pass: the campaign's outputs have no disk replica inside SCDF or at JLab (ASGC and JLab tape only). |

## 2. Output datasets exist before the task is submitted

Every dataset a task's jobs register into exists in JLab Rucio, with
its replication rule and its dataset metadata, before the task is
submitted to PanDA. The job registers files: the replica, the
attachment to the dataset, and the file's `events`. It carries no
dataset metadata and creates no rule.

JEDI creates output datasets and rules for the tasks whose outputs it
handles. ePIC production tasks are submitted `noOutput` and register
their outputs in JLab Rucio themselves, so no output dataset existed
until a job made it: the first job's upload client created the dataset
with its rule and metadata, and every later job repeated the call to be
told the dataset existed. The dataset metadata (software release,
generator, requesting group, Q2 range, beams and species, background
mixing, geometry, data level, gun parameters) is a function of the
task's tags and production configuration, so it is known at submission.

### The pre-submission step

The submission doer (`submit-evgen-task.py`), after the spec is fetched
and before the kernel submits, creates for each output the task
registers (FULL when `COPYFULL`, RECO when `COPYRECO`, the generated
EVGEN sample of an internal-EVGEN task):

- the dataset DID, named by the payload's naming contract from the
  manifest rows and the environment (`_payload_names`: detector
  version, detector configuration, the try prefix of a residual, the
  EVGEN-relative directory); a trial's datasets under its output root;
  a canary run's single flat dataset;
- one replication rule: account `eicprod`, one copy, the task's output
  RSE, grouping `DATASET`, with the run's lifetime where a trial or
  canary run carries one, as the upload client makes it today;
- the dataset metadata, validated against the payload's schema.

The spec carries the datasets and their metadata as `outputs`, composed
by PCS beside the job environment (`pcs/commands.py`). The client and
credential are the EVGEN registration doer's (`rucio_client(proxy)`,
the `eicprod` proxy), so the three writers into the JLab catalog share
one identity.

A dataset that already exists is accepted: its rule is added if it has
none, and its metadata is set where absent. Metadata that differs from
the task's is a conflict, since the same output name would hold
different physics, and the submission stops with the reason. A catalog
that cannot be reached, or refuses a creation, stops the submission
with the reason before any task is submitted; the outcome is on the
`evgen_task_submit` action.

The created DIDs are recorded on the task's submission record
(`PandaTasks.metadata.output_datasets`). For an epicprod task this is
the explicit Rucio reference that EPICPROD_DATA_LINEAGE.md gathers
after the fact for data produced before PanDA; here it is written at
submission.

### The job's registration

`register_to_rucio.py` uploads the file and attaches it to the existing
dataset; it passes no dataset metadata and no lifetime, and a missing
dataset is a registration failure (exit 78), since a task whose
datasets were not created was not submitted through this path. The
canary and trial DID lifetimes stay on the file DIDs as today.

The job still extracts metadata from its own output file
(`parse_podio_metadata.py`, `eic-info`) and compares it with the
dataset's. A value that differs, or a dataset value that is absent, is
written to the stage log and to the payload report's registration
outcome as a metadata comparison, and is never a failure: the dataset
carries what the task declared, and the job reports whether the
software that ran agrees.

### Validation

A payload canary run on one manifest row, with the datasets inspected
in Rucio before the job runs (DID, rule, metadata) and the comparison
read from the payload report after; then a trial task the same way.

## Enforcement

The EVGEN inventory assimilation records each dataset's `events` from
Rucio and the EVGEN inputs page shows it, so a registered dataset
without a count is visible as such. The same visibility for produced
data follows when its registration path writes the count. The
pre-submission step's record on the task is the check that a task's
datasets existed before its jobs ran; the payload report's metadata
comparison is the check that they carried the right values.
