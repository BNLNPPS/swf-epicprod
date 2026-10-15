# Automatic deployment of swf-epicprod

A change merged or pushed to `main` of BNLNPPS/swf-epicprod is deployed
to production on swf-testbed within moments, without a manual step.
Contributors open a pull request from their fork and merge it
themselves; no review is required. The deploy follows the merge.

## Mechanism

A GitHub Actions self-hosted runner on swf-testbed (label
`swf-testbed`, account `wenauseic`, systemd service) holds an outbound
connection to GitHub; the host is inside the SCDF perimeter and accepts
no inbound webhook. A push to `main` starts the `deploy` workflow
(`.github/workflows/deploy.yml`) on that runner, which runs
`scripts/autodeploy.sh`:

1. Pulls the dev tree `/data/wenauseic/github/swf-epicprod`. The
   lightweight deploy syncs from this tree and the full deploy freezes
   it into the release, so it is the deploy's source.
2. Takes the paths changed between the last deployed commit, recorded
   in `/opt/swf-monitor/shared/epicprod-deployed-commit`, and the new
   head. A run missed or refused is therefore covered by the next one.
3. Chooses the deploy with `scripts/autodeploy-choose.sh`.
4. Refuses when a tree the deploy ships from (swf-epicprod,
   swf-monitor, snapper-ai, site-canary, swf-common-lib) has
   uncommitted or unpushed work, so no session's work in progress
   goes live.
5. Runs the deploy, compares every changed package file with its
   deployed copy, checks the monitor's health endpoint, and records the
   new commit.

Every refusal and failure exits nonzero: the run is red in the
repository's Actions tab, and GitHub notifies the author of the push.
Each run also appends to
`/opt/swf-monitor/shared/logs/epicprod-autodeploy.log`. A refused run
is repeated with **Run workflow** on the `deploy` workflow, or by
`bash scripts/autodeploy.sh` on swf-testbed, once its cause is
cleared. Runs are serialized by the workflow's concurrency group and a
lock file.

## Which deploy

| Changed paths | Deploy |
|---|---|
| `docs/`, `*.md`, `tests/`, `.github/` | none |
| `scripts/`, `tools/`, `pilot/`, `perlmutter/`, top-level `gke/` | none: run from the dev tree or deployed by their own procedure; the run lists them |
| `pcs/` (templates, views, forms, urls and the rest) | lightweight `--ui` |
| `swf_epicprod/analytics/`, `swf_epicprod/assessment/` | lightweight `--ui` |
| `swf_epicprod/mcp_tools/` | lightweight `--ui --mcp` |
| a `static/` asset under a lightweight deploy | adds `--static` |
| `pcs/management/`, `pyproject.toml`, the rest of `swf_epicprod/` (the payload and every module the ops agent imports) | full |
| `pcs/migrations/`, `pcs/models.py` | none: the run stops |
| any other path | full |

The largest need in the range decides. The ops agent loads new code
only at its restart, which only the full deploy performs, so anything
it imports takes the full deploy. A payload change applies to tasks
submitted after the deploy: the submission copies the payload from the
release into the task's sandbox
([EPICPROD_PAYLOAD.md](EPICPROD_PAYLOAD.md)).

A schema change is never deployed automatically. The run stops, and
the migration is deployed by hand on explicit approval; the next run
then covers the range.

## Preview on the pull request

The `deploy-preview` workflow runs on every pull request to `main`, on
GitHub's own machines, and reports which deploy the merge will trigger
as a notice on the PR's checks. It reads only the diff. The deploy
workflow has no `pull_request` trigger and must never be given one:
code from a fork must not run on the production host.

## Runner

Installed in `/data/wenauseic/actions-runner`, registered to the
repository with the label `swf-testbed`, and run by the systemd unit
the runner's `svc.sh` installs. The account has passwordless sudo,
which both deploy scripts require.
