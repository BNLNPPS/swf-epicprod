# Analysis runs

How an ePIC analysis runs on PanDA through epicprod, from cards: the
`analysis` application, the job, and the contracts between them, the
analysis repository and the operations agent.

## Flow

1. **Tag.** An analysis card at a commit of the analysis repository is
   registered as an `AnalysisTag` (`a1`, `a2`, ...).
2. **Dataset card.** A sample's delivered files are frozen into a
   `DatasetCard`: one file per work unit, the latest try.
3. **Run.** A tag on dataset cards of one campaign, with exclusions,
   parameter overrides and a purpose (`user` or `validation`), is frozen
   into a run card; its sha256 is the run's id.
4. **Map task.** One job per manifest row runs the card's map stage on its
   files and uploads its partial to the run's scratch.
5. **Merge task.** Once every map job has reported, one job per sample
   merges its partials, runs finalize and publishes each plot with its
   sidecar, the result card.

Job reports reach the application through the operations agent:
object storage, then the REST endpoints below.

## Pieces

| Piece | File |
|---|---|
| Card rules (pure functions) | `analysis/cards.py` |
| Records: `AnalysisTag`, `DatasetCard`, `AnalysisRun`, `AnalysisJobOutput` | `analysis/models.py` |
| Register, freeze, task specifications, report intake | `analysis/services.py` |
| REST, mounted at `analysis/` | `analysis/api_views.py`, `analysis/urls.py` |
| The job | `swf_epicprod/payload/analysis_run.sh`, `analysis_job.py` |

The application reads PCS through one link: a dataset card names the PCS
`Dataset` whose `DeliveredOutput` rows it froze. PCS does not refer to
analysis.

## Identity

- **Tag.** Repository, commit, card path, the card verbatim with its sha256,
  and the image. Fixed once a run uses it; a new commit is a new tag.
- **Dataset card.** Sample and revision, from delivered outputs
  (`source=delivered`) or a Rucio listing (`source=rucio`). An unchanged
  file list refreezes to the same revision.
- **Run card.** The tag's pin, the inputs (sample, revision, manifest
  sha256), the exclusions, the effective parameters and the split.
  `run_card_id = sha256` of its canonical JSON. Site, memory, purpose and
  owner are outside it, so freezing the same choices returns the existing
  run, and a validation copy of a user's run has the same id.

## The analysis card

Kept in the analysis repository; registration checks these fields
(`cards.check_analysis_card`). Example: `eic/physics_benchmarks`
`benchmarks/SIDIS/dihadron/card.yml`.

```yaml
identifier: "epic:benchmark:SIDIS/dihadron"
eic_shell: {container: eic_xl, version: 26.07.3-stable}   # the image the jobs run
parameters: {DIHADRON_TRIG_LOW: 2, MODE: reco}            # defaults; a run may override
split: {files_per_job: 11}
stages:                                                   # run in the repository
  map:      'snakemake --cores 1 --config $PARAMS -- results/dihadron/$SAMPLE/$MODE/job_$JOB.root'
  merge:    'snakemake --cores 1 --allowed-rules dihadron_merge --config $PARAMS -- ...merged.root'
  finalize: 'snakemake --cores 1 --allowed-rules dihadron_finalize --config $PARAMS -- ...plots'
files:
  list:    results/dihadron/$SAMPLE/lists/list_$JOB.txt   # map: the job writes its URLs here
  partial: results/dihadron/$SAMPLE/$MODE/job_$JOB.root   # map: the partial result
  hists:   results/dihadron/$SAMPLE/$MODE/hists.json      # finalize: {name: {entries, bins}}
  plots:   results/dihadron/$SAMPLE/$MODE/plots           # finalize: *.png
auxiliary:                                                # other inputs, path or url
  - {url: https://github.com/eic/epic-data/raw/<commit>/..., sha256: ...}
```

A stage command gets `$SAMPLE` (the sample path's last three components
joined by `_`), `$JOB` (map), `$PARAMS` (the parameters as sorted `K=V`
words; each value one plain word) and each parameter by name. Merge and
finalize run without the data or the map stage's build products, so they
must not remake them.

## The job

`bash payload/analysis_run.sh %RNDM=0 <csv base>` sets up Rucio as `run.sh`
does and runs `analysis_job.py` on row `%RNDM` of `<csv base>.csv`. Only
analysis tasks run it.

| Stage | Row | Does |
|---|---|---|
| map | `seq;sample;job;did;...` | Fetch the repository at the commit; refuse a card whose sha256 differs from the tag's; resolve the DIDs at BNL-XRD, then EIC-XRD; write `files.list`; run `stages.map`; upload `files.partial` to scratch |
| merge | `k;sample;merge;job=did;...` | Fetch the repository; read the sample's partials back with a storage read token and check each adler32; run `stages.merge` and `stages.finalize`; publish each plot and its sidecar |

Each job writes `analysis-report.json` (`epicprod-analysis-report/1`: the
partial and its adler32, or the result card) and sends it with
`report_out.py` to `analysis/<run id 12>/<stage>_<seq>.json` when job
reporting is configured. `ANALYSIS_LOCAL=1` runs the same steps with no
Rucio, storage or report: DIDs resolve under `ANALYSIS_URL_PREFIX` and the
store is the directory `ANALYSIS_LOCAL_STORE`.

## Storage

Unregistered uploads to EIC-XRD-LOG (`register_to_rucio.py --noregister`):
DID name `/X` lands at `/work/eic3/EPIC/eic/X`. `<root>` is `validation`
for a validation run and the owner's name for a user's run.

| What | DID name |
|---|---|
| Partial | `/<root>/scratch/<run id 12>/<sample>.job_<job>.root` |
| Plot | `/<root>/images/campaigns/<campaign>/<sample>.<plot>.png` |
| Sidecar | `/<root>/images/campaigns/<campaign>/<sample>.<plot>.json` |

`<campaign>` is the samples' release without dots (`26.07.3` → `26073`).

## Task specifications

`services.build_map_spec(run)` and `build_merge_spec(run)` return what the
operations agent's doer submits, in the shape of
`pcs.commands.build_evgen_task_params`:

| Key | Value |
|---|---|
| `kind`, `stage` | `analysis`; `map` or `merge` |
| `runCardId`, `outDS` | The run's id; `analysis.<tag>.<run id 12>.<stage>` |
| `vo`, `userName`, `workingGroup`, `nEventsPerJob`, `skipScout`, `disableReassign` | `epic`, the run's owner, `EIC`, 1, true, true |
| `prodSourceLabel`, `taskType`, `processingType` | `user`, `anal`, `epicanalysis` |
| `gshare` | `Validation` or `User`, from the run's purpose |
| `site`, `memory`, `walltimeHours`, `nCore` | From the run; default `BNL_PanDA_1`, 2000 MB, 4 h, 1 |
| `containerImage` | The tag's image |
| `csvBase`, `csvRows`, `nJobs`, `exec` | The manifest rows and `bash payload/analysis_run.sh %RNDM=0 <csvBase>` |
| `env` | `ANALYSIS_STAGE`, `ANALYSIS_RUN_ID`, `ANALYSIS_REPO`, `ANALYSIS_COMMIT`, `ANALYSIS_CARD`, `ANALYSIS_CARD_SHA256`, `ANALYSIS_PARAMS`, `ANALYSIS_SCRATCH`, `ANALYSIS_PUBLISH`, `ANALYSIS_RSE` |
| `sandboxFiles` | Merge only: `analysis_run_card.json`, `analysis_card.json`, `analysis_jobs.json`, `analysis_run.json`, written beside the csv |

The merge specification refuses to build until every map job has a
written partial.

## REST

| Method | Path | Auth | Does |
|---|---|---|---|
| GET | `analysis/api/v1/dataset-card/?sample=RECO/...&revision=N` | none | A dataset card's header; `&manifest=1` for the gzipped manifest |
| GET | `analysis/api/v1/runs/<run id>/` | none | Run card, status, PanDA tasks, result |
| POST | `analysis/api/v1/runs/<run id>/job-outputs/` | token | `{"outputs": [{seq, did, adler32, bytes, ...}]}`; idempotent per job, confined to the run's scratch |
| POST | `analysis/api/v1/runs/<run id>/result/` | token | One sample's result card; the run is done when every sample has one |

## Deployment

Models and migrations deploy by hand: `scripts/autodeploy-choose.sh` stops
on `analysis/models.py` and `analysis/migrations/`. swf-monitor needs
`analysis` in `INSTALLED_APPS` and `path("analysis/", include("analysis.urls"))`.

## Not wired yet

- The doer and agent handler that submit a specification; the ingest that
  posts job reports to the POST endpoints (swf-monitor).
- Pages and MCP tools to register tags, freeze cards and create runs.
- A sweeper for scratch; resubmission of a run's failed jobs.
