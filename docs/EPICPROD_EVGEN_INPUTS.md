# ePIC Production EVGEN Inputs

A production task reconstructs a generator-level event sample. That sample —
the **EVGEN** (event-generation) input — is produced by a physics working group
and registered in Rucio. This document describes where EVGEN inputs live, how
PCS (Physics Configuration System) assimilates the Rucio inventory, how it
resolves each catalog request to the Rucio dataset(s) that realize it, and how
to read the matched and unmatched result. Matching is the part that takes
judgment; most of this document is about it.

## Where EVGEN inputs live

EVGEN datasets are registered in **JLab Rucio**, scope `epic`, under
`/EVGEN/...`. They are detector-independent — one EVGEN sample feeds any
detector configuration — so the tree carries no detector or campaign-version
segment. The files are HepMC3 (`*.hepmc3.tree.root`) and commonly reside on
tape (`JLAB-TAPE-SE`), so staging them incurs a tape recall.

Read access uses the public `eicread` userpass; no production credential is
needed to list or inspect EVGEN. PanDA does not resolve EVGEN through Rucio at
all: a PanDA server is bound to a single Rucio instance (BNL Rucio for the BNL
server), and the production payload stages its input from JLab Rucio itself. See
[JEDI_INTEGRATION.md](JEDI_INTEGRATION.md) § "Data handling and the single-Rucio
constraint".

## The two namespaces: request and Rucio

A PCS catalog request names an EVGEN path under
`/volatile/eic/EPIC/EVGEN/<tail>`, taken from the production team's
`default_datasets` catalogue (`eic/epic-prod`, the basis of the PCS task
catalog). The produced sample is registered in Rucio as `epic:/EVGEN/<tail>`.
The `<tail>` is the same namespace on both sides, but the request tail ranges
from abstract to fully specific depending on physics class:

| Class | Request tail | Rucio DID tail |
|-------|--------------|----------------|
| DIS (pythia8) | `DIS/NC/10x100/minQ2=1` | `DIS/pythia8.316-1.0/NC/noRad/ep/10x100/q2_1to10` |
| SIDIS (pythia6) | `SIDIS/pythia6-eic/1.2.0/ep_noradcor/18x275/q2_1to10` | `SIDIS/pythia6-eic/1.1.0/en_noradcor/18x275/q2_1to10` |
| EXCLUSIVE (DEMP) | — | `EXCLUSIVE/DEMP/DEMPgen-1.2.3/10x130/q2_10_20/pi+` |
| DIS (BeAGLE, nuclear) | `DIS/BeAGLE1.03.02-1.0/eH2/10x130` | `DIS/BeAGLE1.03.02-1.0/eAu/5x41/q2_1to10` |

A DIS pythia8 request states only the current type (`NC`/`CC`), beam, and a Q²
floor; the Rucio DID additionally carries generator, radiation, and charge, and
a Q² range rather than a floor. A SIDIS request, by contrast, already carries
generator, version, charge, radiation, and an explicit Q² range. The match must
respect whichever axes a request actually states.

## Assimilation

`refresh_evgen_rucio` (`src/pcs/services.py`) fetches `epic:/EVGEN/*` once into a
snapshot, resolves each PCS evgen `Dataset` to the Rucio dataset(s) it matches,
and writes the resolved references onto `Dataset.metadata['rucio']`. Re-running
picks up a grown Rucio listing the same way — assimilation is idempotent and
re-sweepable.

- Each `metadata['rucio']` entry records the resolved Rucio `did`, `file_count`,
  `bytes`, per-RSE availability, and completeness.
- The standalone runner is `scripts/import_evgen_rucio.py`: a dry run by default
  (fetch, match, and report with no database writes) and `--apply` to persist.
  The same service backs the catalog's update button (run under the production
  operations agent, the same pattern as the produced-output sweep).
- The snapshot is written as one JSON file under the snapshot directory.

## Matching

A request resolves to a Rucio dataset when the request's path tokens appear, in
order, as a subsequence of the Rucio DID's tokens, compared **exactly except for
the Q² token**. Two consequences follow, and they are the whole point:

- **Exact comparison on every axis the request states.** A request that names a
  charge, generator, or version matches only a DID carrying the same value:
  `ep` never matches `en`, `pythia6-eic/1.2.0` never matches `1.1.0`. This is
  what keeps a specific request off the wrong beam species or generator version.
- **Fan-out for every axis the request omits.** An abstract DIS request states
  no generator, radiation, or charge, so it matches every Rucio dataset that
  agrees on the axes it does state. One request resolves to several datasets.

### Q² semantics

The Q² token is the one axis compared by value, not string:

- An explicit request range (`q2_1to10`) matches only the identical Rucio range.
- A Q² floor request (`minQ2=N`) matches every Rucio range lying entirely at or
  above the floor. `minQ2=10` resolves to `q2_10to100` and `q2_100to1000`, never
  to `q2_1to10` (which would include events below the floor). `minQ2=1` resolves
  to all three ranges.

### Version policy

A requested generator version absent from Rucio is left unmatched and surfaced
as a gap. It is never substituted with a different version.

### Separate from produced-output matching

Input matching is implemented independently of the produced-output match
(`EPICPROD_DATA_LINEAGE.md`, `_filter_match`/`_q2_overlap`). Output matching
deliberately tolerates the abstract-request-to-specific-output gap and treats Q²
as overlapping; input matching requires exact axes and exact-or-floor Q². The
two policies share no code, so a change to one cannot alter the other.

## Reading the result

Every assimilation yields three populations, and an operator should be able to
see all three:

- **Matched** — a request resolved to one or more Rucio datasets, recorded on
  `Dataset.metadata['rucio']`. These are the runnable inputs.
- **Unmatched request** — a catalog request with no Rucio dataset. The requested
  sample is not yet produced or registered, or it differs from what is
  registered (a different version or charge). This is expected during
  commissioning; it is the completeness signal, not an error.
- **Unmatched Rucio** — a registered EVGEN dataset that no request claims.
  Either it is produced outside the catalogue, or the catalogue spells the
  request differently.

Both unmatched populations are discoverability targets for the catalog UI: an
operator reconciles them by adding or correcting a request, or by registering
the missing data.

## Current state

Implemented: the assimilation sweep, the input matcher, and the catalog
"Update EVGEN from Rucio" button (the production operations agent runs the sweep
with apply and the page refreshes on completion). On the assimilated inventory
the matcher resolves the DIS NC pythia8 samples (with the Q² fan-out above) and
one beam-gas background; SIDIS and other classes fall to unmatched where the
registered version, charge, or class differs from the request, as designed. Not
yet implemented: the catalog UI surfacing of the matched and unmatched
populations, and consuming a matched EVGEN dataset as a payload-staged
submission input.

## Related

- [JEDI_INTEGRATION.md](JEDI_INTEGRATION.md) — submission design; the single-Rucio constraint and the payload-staged input mode.
- [EPICPROD_DATA_LINEAGE.md](EPICPROD_DATA_LINEAGE.md) — the produced-output sibling: gathering RECO/FULL Rucio references onto the catalog.
- [EPICPROD_TASK_CATALOG.md](EPICPROD_TASK_CATALOG.md) — the production task catalog and its filters.
- [PCS.md](PCS.md) — the configuration and dataset-identity model.
