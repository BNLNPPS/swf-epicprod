# PCS Background Tag (`k`)

The fifth PCS tag type, prefix `k`. It names a background configuration —
beam-gas, synchrotron radiation, or other pre-generated overlay samples — as a
named, reusable tag, independent of any physics signal, taking its place in the
dataset name alongside `p`, `e`, `s`, `r`.

## Purpose

Background is generated independently of any physics signal and mixed into many.
It is not a physics process tied to a particular signal — that
independence is why it is a tag type of its own rather than a field on the
physics or evgen tag. Folding background into the physics tag would multiply the
physics tags by every background; a separate overlay keeps the count additive,
not combinatorial. One background definition is reused across every signal it
overlays.

Historically, the execution fields were spread across `EvgenTag`
(`signal_freq`, `signal_status`, `bg_tag_prefix`, `bg_files`) and `ProdConfig`
(`bg_mixing`, `bg_cross_section`, `bg_evtgen_file`), unnamed and unversioned.
The `k` tag gathers the background-specific fields into one locked record and
gives them a place in the dataset identity. Runtime command generation prefers
the `k` tag when it is specified and keeps the EvGen fields as a legacy fallback.

## Why `k`

A distinct letter keeps physics tags decimal, rather than folding a background
digit into `p` and forcing rethinking numbering (e.g. hex). `b` is already the block suffix
(`.b1`, `.b2`).

## Schema

In `pcs/schemas.py`. Field values are open strings; the listed values are form
suggestions, not a closed set, so the path parser passes through whatever a
future sample names.

- Required: `background_type` (e.g. `BEAMGAS`, `SYNRAD`).
- Sample-defining, populated by the campaign import: `bg_source` (e.g.
  `electron`, `proton`), `bg_mechanism` (e.g. `brems`, `coulomb`, `touschek`;
  blank when the path names a generator instead), `bg_generator` (generator/tool
  and version/release), `beam_energy_electron`, `beam_energy_hadron`.
- Overlay/mixing, for a background mixed into a signal: `cross_section`,
  `signal_freq`, `signal_status`, `bg_tag_prefix`, `bg_files`, `evtgen_file`.
  Also `beam_species`, `notes`.

The beam energies the background was generated for live here, on the background
tag; for a standalone background sample the physics slot is the signal-free
`p6001` tag, which carries no beam.

## Import

The campaign importer parses each EVGEN backgrounds path into these parameters,
resolves or creates the `k` tag (`find_or_create_background_tag`, matching the
sample-defining fields), and binds the dataset to `p6001` plus that `k` tag.
A single 4th segment that is not a known mechanism is taken as the generator; a
bare `NGeV` beam is assigned to the electron or hadron beam by source.

## Behavior

`k` is structurally identical to `e`/`s`/`r`: sequential labels `k1`, `k2`, …,
`draft → locked`, creator-owned, no categories. A dataset carries at most one
background tag, set independently of its physics tag.

When a production config enables background mixing, the submission environment
uses the dataset's background tag parameters first. `bg_tag_prefix` becomes
`TAG_PREFIX`; `bg_files` becomes `BG_FILES` and is staged into the EVGEN sandbox
by the submit doer. If the dataset has no usable `k` value, PCS falls back to
the same parameters on the EvGen tag for older definitions.

## The no-signal physics tag

Every dataset names a physics tag. A standalone background sample has no signal,
so it names `p6001` — a single physics tag in a new category 6, created once,
all parameters blank. It means "no physics signal; see the background
tag." A dataset that mixes background into a real signal keeps that signal's
physics tag and adds a `k`.

During alpha commissioning, `k` tags and `p6001` are created `draft`, not
`locked` — like every other PCS tag — so ops can shape the campaign-to-tag
mapping freely; reproducibility locking moves to submission prep. See
[Commissioning Relaxations](COMMISSIONING_RELAXATIONS.md).

## Dataset name

`k` appends after reco, before the Rucio block:

```
{scope}.{detector_version}.{detector_config}.{p}.{e}.{s}.{r}.{k}   →   DID …{k}.b{N}
```

The segment is present only when the dataset carries a background.
