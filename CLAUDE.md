# swf-epicprod

Start from the SWF workspace guide, `../swf-testbed/CLAUDE.md`
(<https://github.com/BNLNPPS/swf-testbed/blob/main/CLAUDE.md>): the
system, the workspace, how to work, production and collaboration. This
file adds what is particular to swf-epicprod, the production domain of
the platform: PCS (the Physics Configuration System), the epicprod
payload, the production operations and their documentation. `README.md`
gives the architecture and the relationship to swf-monitor and
swf-common-lib.

## Required reading

Before PCS or epicprod work, read these in `docs/`, in full for the
area being changed: ARCHITECTURE_MAP (the plan of record for what lives
where and each component's interface), PCS, PCS_DATASET_REQUEST_WORKFLOW,
EPICPROD_TASK_CATALOG, JEDI_INTEGRATION, EPICPROD_OPS_AGENT, EPICPROD_OPS,
EPICPROD_DATA_LINEAGE, EPICPROD_EVGEN_INPUTS, EPICPROD_ASSESSMENTS and
COMMISSIONING_RELAXATIONS.

- Retries, reruns and ghosts: EPICPROD_RETRIES.md before any answer.
- Job failures: swf-monitor `docs/ERROR_ATTRIBUTION.md` and
  `docs/SNAPPER_ERRORS.md` (the error-state record its corrections are
  read through), before the production documents.
- The payload: EPICPROD_PAYLOAD.md, and DEVELOPING_THE_PAYLOAD.md for a
  change to it (switches, developer payload canaries).

`README.md` is the one index of the documentation set; a new document is
added there. Platform-service documents (action stream, SSE, external
access, MCP, deployment) are in swf-monitor's `docs/`.

## Git

Maintainers commit to `main`. Outside contributors open a pull request
from a fork and merge it themselves; there is no review gate. This
repository is not in the coordinated `infra/baseline-vNN` set.

## Deployment

A push or merge to `main` deploys itself: a host-side timer polls canonical
`main` every minute and runs `scripts/autodeploy.sh`, which chooses no deploy, a
lightweight one or a full one from the changed paths, and stops on a
change to `pcs/models.py` or `pcs/migrations/`, which is deployed by hand
on explicit approval ([docs/AUTODEPLOY.md](docs/AUTODEPLOY.md)). A push
is therefore not deployed again by hand; check its `deployment/swf-testbed`
commit status. PR checks run on GitHub-hosted machines; production is not
registered as an Actions runner. A payload change reaches tasks submitted after the deploy.

## Findings

Material production findings and resolutions are reported in
`epicprod-live` with the affected scope, the current status and the
evidence; private dialog, LLM messages and commits are not substitutes.
The discipline is in swf-monitor `docs/NOTICE_ROUTING.md` (Findings and
resolutions).

## The pcs application

The `pcs` Django application lives here (top-level `pcs/`) and is
installed into the swf-monitor runtime; its import path, app label and
`pcs_*` tables are those of its swf-monitor origin, where its history
before 2026-07-10 remains. On the production host, maintainers iterate
with `sudo ../swf-monitor/deploy-lightweight-ui-mcp.sh --ui`, which syncs
this tree onto the deployed copy; migrations and management commands
take the full swf-monitor deploy.
