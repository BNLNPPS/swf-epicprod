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
(`EPICPROD_*.md`, PCS docs) migrates here from `swf-monitor/docs/`;
until a doc has moved, the swf-monitor copy is authoritative.
