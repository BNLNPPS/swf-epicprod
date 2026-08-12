# Composed-Name Collision Families — Backfill Review

The per-family decision table for
[PCS_COMPOSED_NAME_INTEGRITY.md](PCS_COMPOSED_NAME_INTEGRITY.md) step 3,
generated 2026-08-11 by `scripts/audit_composed_name_families.py` from
the live catalog. Colliding sample-less datasets: 133 taskname-born in
35 families, 4,231 past.* assimilated, 29 unparsable. Each group below
carries a recommended disposition; the decision column is the
operator's. Dispositions: **sample** (a `sample_name`, no tag change),
**tag** (new or existing physics tags via extended derivation),
**evgen** (the discriminator belongs on the EVGEN tag), **k-tag**
(background-mix axis), **rule** (mechanical, no per-family judgment).

## Decisions (operator, 2026-08-11)

| Group | Disposition |
|---|---|
| 1 — DIS minQ2 scans | tag (extend q2 recognition, per-combination tags) |
| 2 — background-mixed variants | **k background tag** |
| 3 — DVCS polarization variants | sample |
| 4 — single-particle scans | sample (precedent) |
| 5 — ALP mass points | tag, via the decimal-token parsing fix |
| 6 — DVMP generator versions | evgen tag |
| 7 — beam-gas machine settings | sample |
| 8 — distinct-tag families | no action beyond the past.* rule |
| past.* class | mechanical sample from source slug |
| unparsables (29) | manual assignment at dry-run review |

These decisions are the backfill dry-run specification.

The dispositions were executed 2026-08-11 by
`scripts/backfill_composed_name_samples.py` in two passes (the second extended the slug rule class-wide after
physics rebinds surfaced collisions with unsampled past.RECO rows
outside the first target set): 77 physics rebinds, all reusing
existing tags; two OVERLAY background tags bound to 16 datasets; five
EVGEN rebinds; 50 discriminator samples; ~5,640 mechanical slug
samples. Composed-name collisions catalog-wide after execution: zero.

## Group 1 — Legacy DIS minQ2 scans (3 families, 54 datasets)

`DIS.NC.NxN.minQN-N` (38), `DIS.CC.NxN.minQN-N` (8), and their
background-mixed twins (14, see Group 2). The `minQ2-{1,10,100,1000}`
token is a q2 threshold — the same physics axis the pythia families
carry as `q2_1to10`-style ranges, in a token shape `derive_physics`
does not recognize. Combinatorics: 4 minQ2 values × 5 beam settings,
recurring across NC/CC and two campaigns — structural, not scan
labels.

**Recommendation: tag.** Extend the q2 token recognition to the
`minQ2-N` shape and bind each (process × beam × minQ2) to its physics
tag via the normal find-or-create — the direct analogue of the
existing per-q2-range pythia tags (p2222…). Roughly 20 new tags.

## Group 2 — Background-mixed variants (2 families, 16 datasets)

`Bkg_ExactNS_Nus.GoldCt.Num.` prefixed DIS and SIDIS names. The prefix
encodes background mixing (Upsilon exact states, gold contamination) —
the axis the composed-name model reserves for the `k` background tag.
These collide both within their families and with their unmixed twins
(56 cross-pairings with Group 1).

**Recommendation: k-tag if the deferred background-tag program is
ready to carry it; sample otherwise.** The sample form (the `Bkg_…`
string verbatim) is cheap, reversible by later automatch, and blocks
nothing; the k-tag form is the model's intended home for this axis.

## Group 3 — DVCS polarization variants (13 families, 26 datasets)

`EXCLUSIVE.DVCS_ABCONV…` with process variants (BH_ONLY, DVCS_BH,
DVCS_ONLY) and beam-polarization final tokens (`emhTm`, `ephLp`, …),
all bound to p3372/p3373 per beam and colliding with each other.
Combinatorics: ~3 process variants × 8 polarization states × beams —
structurally recurring, but a tag-per-combination roughly doubles the
DVCS tag count for one analysis family.

**Recommendation: sample** (the process-variant and polarization
tokens verbatim), on combinatorics: the explosion outweighs the axis
regularity. Tag treatment remains available later via automatch if
polarized DVCS becomes a standing production line.

## Group 4 — Single-particle scans (6 families, 15 datasets)

`SINGLE.<particle>.NMeV.NtoNdeg`. The angle range is already declared
a sample variant by design (`derive_physics` excludes it, and the 416
existing sample names — `45to135deg` etc. — are exactly these).

**Recommendation: sample** (the angle token), conforming to the
established precedent.

## Group 5 — ALP mass points (2 families, 6 datasets)

`EW_BSM.ALP…ma_N.N`. Mass is an existing physics axis, but the
decimal token `ma_0.1` fragments at the name's dot-split before
derivation sees it, so all mass points collapsed onto one tag per
channel.

**Recommendation: tag, via a parsing fix** — recognize decimal-valued
tokens (`ma_0.1`) before the dot-split and bind per-mass tags through
the existing mass axis. Small combinatorics (3 masses × 2 channels).

## Group 6 — DVMP generator versions (2 families, 4 datasets)

`EXCLUSIVE.DVMP.EpICN.N.N-N.N…` where the varying token is the EpIC
generator version (`6-1` vs `8-1`). Generator identity is an EVGEN-tag
axis by the model's own division; these datasets share the anchor
evgen tag because auto-intake anchors it.

**Recommendation: evgen** — bind the generator version into distinct
EVGEN tags (e-axis), leaving physics and sample untouched. Smallest
group, but it exercises the third axis correctly.

## Group 7 — Beam-gas machine settings (2 families, 6 datasets)

`BACKGROUNDS.BEAMGAS…dataprod_rel_N.N.N.NxN.NAhr.MachineRuntimeNs`.
Signal-free physics by design (p6001 + background domain); the
discriminators are machine-configuration strings.

**Recommendation: sample** (the release/current/runtime token string).

## Group 8 — Already-distinct tags colliding only with past.* rows

The pythia q2 families and several others carry correct distinct tags;
their only collisions are with hash-named past.* rows sharing the same
tag. Resolved entirely by the past.* rule below.

**Recommendation: no action** beyond the past.* rule.

## The past.* assimilated class (4,231 datasets) — rule decision

Hash-named archival rows from pre-PCS campaigns; no physics remainder
exists to judge.

**Recommendation: rule** — mechanical `sample_name` from each row's
unique source slug. One decision, fully programmatic, and it removes
the numerical bulk of the collisions.

## Unparsable names (29 datasets)

Listed individually in the generated appendix; each gets a manual
sample assignment during the backfill dry-run review.

## Appendix — generated audit

The full per-family audit (members, varying tokens, capture status,
dry-run tag matches, collision partners) is regenerated at any time by
`scripts/audit_composed_name_families.py`; the backfill dry run
consumes the same grouping.
