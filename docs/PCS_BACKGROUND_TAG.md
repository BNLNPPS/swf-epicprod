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
The EvGen compose form hides those legacy fields; they appear only when an
existing tag that has one of them filled is edited or copied. New background
definitions go on a `k` tag.

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

A dataset that carries a background tag over a real signal mixes: the `k` tag
in its identity says so, and a run without the mix would be published under a
name that claims it. The task's effective configuration therefore has
`bg_mixing` on, noted as coming from the background tag, whatever its
configuration or overrides say; a standalone background sample, on the
signal-free `p6001`, mixes nothing. The configuration's `bg_mixing` remains the
switch for older definitions that keep their background on the EvGen tag.

When a task mixes, the submission environment uses the dataset's background tag
parameters first: `signal_freq` and `signal_status` become `SIGNAL_FREQ` and
`SIGNAL_STATUS`, `bg_tag_prefix` becomes `TAG_PREFIX`, and `bg_files` becomes
`BG_FILES`, the name of the JSON of background sources and frequencies the
job mixes. The task is not ready until its background tag carries both
`bg_files` and `bg_tag_prefix`: the files say what to mix, and the prefix keeps
the mixed output out of the signal-only sample's paths. If the dataset has no
usable `k` value, PCS falls back to the same parameters on the EvGen tag for
older definitions.

### The sources JSON is held in PCS

The JSON's contents are stored on the tag beside its name, as
`bg_files_content` (`{file, sources, loaded_from, loaded_at, loaded_by}`), so
production does not depend on the campaign datasets repository
(eic/simulation_campaign_datasets, to be archived; Sakib Rahman and Torre
Wenaus, 2026-10-22). Submission passes the contents to the submit doer
(`BG_FILES_CONTENT`), which writes them into the sandbox under the `BG_FILES`
name. The contents count only while their `file` is the tag's current
`bg_files`, so a tag pointed at another file needs its contents loaded again,
and until then a task that mixes is not ready ("the background sources JSON
… is not stored in PCS"). `pcs/background_files.py` loads them from the
repository's `config_data` (the eicweb mirror, or a checkout given with
`--source-dir`), once per file: `python -m pcs.background_files [--tag k26]
[--apply]`. Editing a tag on its page keeps the stored contents. A submission
prepared before the contents were stored arrives without them; the doer then
still downloads the file from `config_data`, and its log says so.

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
