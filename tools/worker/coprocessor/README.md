# Coprocessor workflow v0

The dispatcher/agent/executable chain of the coprocessor workflow
([VOLUNTEER_GPU_PLAN.md](../../../docs/VOLUNTEER_GPU_PLAN.md)). The
interface between the pieces is
[WORK_UNIT_CONTRACT.md](../../../docs/WORK_UNIT_CONTRACT.md).

| Piece | Role |
|---|---|
| `dispatcher.py` | serves work units, collects results; sqlite state, stdlib only |
| `worker_agent.py` | worker-side networking: stages units into the inbox, returns results |
| `exec_oneshot.py` | interim contract executable: one `synrad_service` process per unit |

`exec_oneshot.py` pays the geometry-load cost every unit; the
resident-loop service being developed on the Windows track replaces it
behind the same contract, with no change to the dispatcher or agent.

## Smoke run (single host)

```bash
# dispatcher
python3 dispatcher.py --port 8750 --state ~/work/coproc/dispatcher.db \
    --results ~/work/coproc/results &

# executable (interim), against a persisted geometry and simphony install
python3 exec_oneshot.py --work ~/work/coproc/work \
    --prefix ~/work/simphony-synrad-install \
    --geom synrad --geom-cfbase <dir-containing-CSGFoundry> \
    --geom-edition synrad_bench-v1 &

# agent
python3 worker_agent.py --dispatcher http://localhost:8750 \
    --work ~/work/coproc/work &

# enqueue units
curl -s -X POST http://localhost:8750/units -d '{"units": [ <spec>, ... ]}'

# watch
curl -s http://localhost:8750/status
```

Unit specs follow the contract; the gun form needs no input files, the
input form takes a photon `.npy` (for example the reference set's
`inphoton` array, which makes the smoke run a physics check: the counts in
the returned `unit.json` must reproduce the reference values).
