# Automatic deployment of swf-epicprod

A change merged or pushed to `main` of BNLNPPS/swf-epicprod is deployed
to production on swf-testbed within about a minute, without a manual step.
Contributors open a pull request from their fork and merge it
themselves; no review is required. The deploy follows the merge.

## Mechanism

A systemd timer on swf-testbed polls the canonical repository's `main`
every minute. The host accepts no inbound webhook and is not registered
as a GitHub Actions runner. The root-owned installed copy of
`scripts/autodeploy-poll.sh` runs as `wenauseic`. It fetches only
`refs/heads/main` from `https://github.com/BNLNPPS/swf-epicprod.git`,
checks the checkout's origin and upstream, and refuses a dirty checkout,
a branch other than `main`, unmerged local commits or rewritten history.
Only then does it run the checked-out `scripts/autodeploy.sh`:

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

Every refusal and failure exits nonzero. The poller publishes a
`deployment/swf-testbed` commit status on GitHub (pending, success or
failure), also visible on the merged PR. It uses the host's existing
`gh` authentication, which must permit commit-status writes. A status API
failure is logged and retried; it cannot silently produce a green status.
GitHub notification behavior for Actions runs does not apply to this
external status. Read the service journal for poller refusals:

```
journalctl -u swf-epicprod-autodeploy.service
```

The existing deploy log remains
`/opt/swf-monitor/shared/logs/epicprod-autodeploy.log`. Refused changes
are retried on the next timer tick after their cause is cleared; an
operator can also run `sudo systemctl start swf-epicprod-autodeploy.service`.
Locks serialize polls and existing deploys. An unchanged deployed commit
does not rebuild the application; it only retries an outstanding GitHub
status update. Repeated refusals do not post duplicate statuses.

## Which deploy

| Changed paths | Deploy |
|---|---|
| `docs/`, `*.md`, `tests/`, `.github/` | none |
| `scripts/`, `tools/`, `pilot/`, `perlmutter/`, top-level `gke/` | none: run from the dev tree or deployed by their own procedure; the run lists them |
| `pandaserver/schedconfig-overrides.json` | none: the server and Harvester writers synchronize this file from GitHub every five minutes; no monitor/ops restart |
| `pcs/` (templates, views, forms, urls and the rest) | lightweight `--ui` |
| `swf_epicprod/analytics/`, `swf_epicprod/assessment/` | lightweight `--ui` |
| `swf_epicprod/mcp_tools/` | lightweight `--ui --mcp` |
| a `static/` asset under a lightweight deploy | adds `--static` |
| `pcs/management/`, `pyproject.toml`, the rest of `swf_epicprod/` (the payload and every module the ops agent imports) | full |
| `pcs/migrations/`, `pcs/models.py` | none: the run stops |
| any other path | full |

The lightweight deploy syncs the swf-monitor and snapper-ai dev trees
along with this one, so their work that is committed and pushed but
not yet deployed goes live with it; the refusal in step 4 keeps
uncommitted and unpushed work out.

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
as a notice on the PR's checks. It reads only the diff. There is no
production Actions runner for fork-authored workflow YAML to select.
The trust boundary is the merge to `main`: code merged there can deploy,
including changes to the deploy script. This does not add a merge-review
gate or change contributor permissions.

## Installation and cutover

For a host still using the former Actions runner, perform this as an
approved live cutover. The order matters:

1. Check that the existing runner is idle and coordinate a brief deploy
   hold with other maintainers. Stop and disable
   `actions.runner.BNLNPPS-swf-epicprod.swf-testbed.service`, then delete
   runner id `2` from the repository's Actions runner registrations.
   Stopping alone is not the permanent fix. Confirm the runner list no
   longer contains the production host.
2. Merge this branch to `main`. The change is scripts, tests, workflow
   and documentation only, so it requires no application restart. Keep
   the existing deployed-commit record; do not reset it to skip changes.
3. From the clean canonical `main` checkout, install the reviewed files:

   ```bash
   sudo install -d -m 0755 /usr/local/libexec
   sudo install -o root -g root -m 0755 scripts/autodeploy-poll.sh /usr/local/libexec/swf-epicprod-autodeploy-poll
   sudo install -o root -g root -m 0644 scripts/systemd/swf-epicprod-autodeploy.{service,timer} /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now swf-epicprod-autodeploy.timer
   sudo systemctl start swf-epicprod-autodeploy.service
   ```

4. Check the service result, recorded deployed commit, GitHub commit
   status and enabled timer. This first pass should record this change
   as requiring no app deploy, unless other changes have also landed.
   Set the repository's default workflow token to read-only and disable
   workflow-token PR approval. The PR preview already declares
   `contents: read` explicitly. These token settings are additional
   hardening; unregistering the production runner closes the host route.

The installed poller and units are not updated by merging repository
changes: an operator reviews and reinstalls them. The existing account
and sudo policy are unchanged. Root ownership protects the installed
trigger from accidental checkout edits; the deployment account itself
still has the existing host privileges.

If polling needs to be stopped, stop the timer and use the existing
manual deploy procedure from trusted `main`. Do not re-register the
public repository's production runner as a rollback.
