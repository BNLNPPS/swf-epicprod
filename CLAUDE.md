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
lives here, moved from `swf-monitor/docs/` 2026-07-10, each doc
leaving a permanent stub at its old path. **The index of that set is
`README.md`**, annotated and linked; it is the one index, and a new
doc is added there. Required reading beyond the doc you are working
in: `EPICPROD_RETRIES.md` before answering any retry, rerun, or ghost
question.
Platform-service docs (action stream, SSE, external access, MCP,
deployment) remain in `swf-monitor/docs/`. Two of them are required
reading for production work despite living there, because production
failures are diagnosed through them:
[`ERROR_ATTRIBUTION.md`](https://github.com/BNLNPPS/swf-monitor/blob/main/docs/ERROR_ATTRIBUTION.md)
— which PanDA error labels are unreliable and what corrects them, the
evidence grades, and the representative-job dig — and
[`SNAPPER_ERRORS.md`](https://github.com/BNLNPPS/swf-monitor/blob/main/docs/SNAPPER_ERRORS.md)
— the error-state record those corrections are read through. A
question of the form "why are these jobs failing" is answered from
there before the production docs.

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
