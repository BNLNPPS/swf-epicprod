# Work-unit contract

The interface between the worker agent (networking and staging) and the
service executable (GPU transport) in the coprocessor workflow
([VOLUNTEER_GPU_PLAN.md](VOLUNTEER_GPU_PLAN.md)). The agent stages work
units into a directory; the executable consumes them one at a time, holding
geometry and the OptiX context resident across units. Both sides build
against this file. `contract_version: 1`.

## Directory convention

The agent and the executable share a work directory:

```
work/
  inbox/    unit specs, staged by the agent
  outbox/   per-unit result directories, written by the executable
```

- The agent writes a spec as a temporary name and renames it into
  `inbox/<unit_id>.unit.json` (rename is the atomicity mechanism). It keeps
  the inbox a few units deep.
- The executable processes inbox specs one at a time in lexicographic
  `unit_id` order. On completion it writes `outbox/<unit_id>/` containing
  the outputs below, writes the empty marker file `outbox/<unit_id>/done`
  last, and removes the inbox spec.
- On a unit failure it writes `outbox/<unit_id>/error.json` (the failure
  record: stage, message, counts so far) in place of `done`. The executable
  exits nonzero only on conditions that end the process (for example
  geometry load failure).
- The executable may exit cleanly at any unit boundary; restart policy
  (periodic re-initialization, anomaly response) belongs to the launcher.
  An inbox spec with no `done` marker is unprocessed and is simply
  reprocessed: the same spec and seed produce the same output.

## Unit spec — `inbox/<unit_id>.unit.json`

```json
{
  "contract_version": 1,
  "unit_id": "task123-000042",
  "geometry_edition": "synrad_bench-v1",
  "generator": {
    "type": "gun",
    "version": 1,
    "count": 500000,
    "seed": 42,
    "params": { "pos": [0.0, 0.0, 25.0], "dir": [0.0, 0.0, 1.0],
                "emin_kev": 0.3, "emax_kev": 19.4, "fan_mrad": 0.0 }
  },
  "limits": { "max_photons_per_launch": 0 }
}
```

- `unit_id` is unique and sortable; the task identity is embedded in it.
- `geometry_edition` names the persisted geometry the unit requires. The
  executable verifies it against the resident geometry and fails the unit
  on mismatch; a worker's stream carries one edition per process life.
- `generator` is versioned so the source can change without a schema
  change. `type: "gun"` is the pencil gun of `examples/synrad/synrad_gun.h`
  with its parameters. A future `type` covers source-table generation. The
  alternative form `"input": { "path": "photons.npy" }` replaces
  `generator` for file-fed units (certification against the reference set).
- `limits.max_photons_per_launch` slices GPU launches; `0` selects the
  platform default. Windows workers require a finite slice under the WDDM
  watchdog.

## Unit result — `outbox/<unit_id>/`

```
hits.npy      (N_hit, 4, 4) float32 sphoton rows, as synrad_service writes
unit.json     the metadata record below
done          empty marker, written last
```

`unit.json`:

```json
{
  "contract_version": 1,
  "unit_id": "task123-000042",
  "status": "ok",
  "geometry_edition": "synrad_bench-v1",
  "generator": { "echo of the spec's generator or input block": "..." },
  "counts": { "generated": 500000, "wall_absorbed": 500000,
              "reflected": 362156, "on_caps": 11316 },
  "timing": { "generate_s": 0.0, "transport_s": 0.0, "us_per_photon": 0.0,
              "launches": 1, "stage_wait_s": 0.0 },
  "device": { "name": "", "driver": "", "platform": "" },
  "process": { "units_since_init": 1, "rss_mb": 0, "vram_mb": 0 },
  "failures": []
}
```

- `counts` and `timing` carry the bookkeeping PanDA receives; `us_per_photon`
  is transport only, `stage_wait_s` is time spent waiting on the inbox.
- `process` supports the soak measurements: growth in `rss_mb`/`vram_mb`
  across `units_since_init` is the leak signal.
- `failures` records recoverable per-unit anomalies (launch retries, TDR
  events) even when `status` is `ok`.

## Delivery

The agent returns the result directory contents and is responsible for all
transport beyond the shared directory. Aggregation of unit records into the
PanDA job record is the driver's concern, outside this contract.
