# swf-epicprod — Claude Code Guidelines

Production domain of the swf platform; see `README.md` for the
architecture and this repository's relationship to `swf-monitor` and
`swf-common-lib`. Workspace rules — doc-first, scope discipline,
environment — live in the workspace `../CLAUDE.md`.

## Git policy

Direct-to-main while the repository is solo-maintained (the
`swf-remote` precedent). Convert to branches + PRs when a second
contributor arrives. This repository is not part of the coordinated
`infra/baseline-vN` branch set.

## Deployment

Code here reaches production through the shared venv chain: installed
into the `swf-testbed/.venv` during development, carried into the
deployed swf-monitor venv by the standard deploy. Picking up new
swf-epicprod code in production is a deliberate install step, never an
implicit fetch at deploy time.

## Docs

`docs/ARCHITECTURE_MAP.md` is the plan of record for what lives where
(common-lib / swf-monitor-as-platform / swf-epicprod) and each
component's consumption interface. The epicprod documentation set
lives here (moved from `swf-monitor/docs/` 2026-07-10, each doc
leaving a permanent stub at its old path): the PCS docs, the
`EPICPROD_*` set (task catalog, ops, ops agent, data lineage, EVGEN
inputs, questionnaire, validation, narratives, assessments, LLM
operations, succession), `PANDA_USER_JOBS.md`,
`JEDI_INTEGRATION.md`, `JEDI_EPIC_PROPOSAL.md`,
`CAMPAIGN_CONTINUUM.md`, and `COMMISSIONING_RELAXATIONS.md`.
Platform-service docs (action stream, SSE, external access, MCP,
deployment) remain in `swf-monitor/docs/`.

## The pcs application

The `pcs` Django application lives here (top-level `pcs/` package) and
is installed into the swf-monitor runtime — import path, app label,
and `pcs_*` tables are unchanged from its swf-monitor origin, so
migration history and cross-app imports are undisturbed. Its git
history before 2026-07-10 remains in swf-monitor. Iterate with
`sudo /data/wenauseic/github/swf-monitor/deploy-lightweight-ui-mcp.sh --ui`
(syncs this tree onto the deployed venv's installed copy); migrations
and management-command changes require the full swf-monitor deploy,
which freezes this package non-editable into the deployed venv.
