#!/usr/bin/env bash
# Installed outside the checkout; the timer never runs PR workflow code.
set -euo pipefail
export GIT_TERMINAL_PROMPT=0 GH_PROMPT_DISABLED=1

REPO=${EPICPROD_POLL_REPO:-/data/wenauseic/github/swf-epicprod}
SHARED=${EPICPROD_POLL_SHARED:-/opt/swf-monitor/shared}
CANONICAL=https://github.com/BNLNPPS/swf-epicprod.git
REF=refs/epicprod-deploy/main
STATE=$SHARED/epicprod-deployed-commit
POSTED=$SHARED/epicprod-deploy-status
target=

log() { echo "[$(date -u '+%FT%TZ')] $*"; }
status() {
    local state=$1 description=$2 previous=
    [[ ! -f "$POSTED" ]] || previous=$(cat "$POSTED")
    [[ "$previous" != "$target $state" ]] || return 0
    # Do not alternate pending/failure on every retry of a refused commit.
    if [[ "$state" == pending && "$previous" == "$target "* ]]; then return 0; fi
    gh api --method POST "repos/BNLNPPS/swf-epicprod/statuses/$target" \
        -f "state=$state" -f 'context=deployment/swf-testbed' \
        -f "description=$description" \
        -f 'target_url=https://github.com/BNLNPPS/swf-epicprod/blob/main/docs/AUTODEPLOY.md' \
        --silent || { log "ERROR: GitHub $state status could not be published"; return 1; }
    printf '%s %s\n' "$target" "$state" > "$POSTED.tmp"
    mv "$POSTED.tmp" "$POSTED"
}
failed() {
    local result=$?
    trap - EXIT
    if (( result != 0 )); then
        log "ERROR: poll/deploy failed (exit $result); see this service's journal"
        if [[ -n "$target" ]]; then status failure 'Autodeploy needs attention; see the host journal' || true; fi
    fi
    exit "$result"
}
trap failed EXIT
refuse() { log "REFUSED: $*"; exit 1; }

exec 8>"$SHARED/epicprod-autodeploy-poll.lock"
flock -n 8 || exit 0
# Cover checkout preparation with the same lock as manual deployments. FD 9
# is inherited by autodeploy.sh, which keeps this lock through deployment.
exec 9>"$SHARED/epicprod-autodeploy.lock"
flock -n 9 || exit 0
[[ -s "$STATE" ]] || refuse "missing deployed-commit record: $STATE"

# Check raw configuration, not a URL expanded by Git's insteadOf rules.
[[ "$(git -C "$REPO" config --get-all remote.origin.url)" == "$CANONICAL" ]] || \
    refuse 'origin must be the canonical BNLNPPS/swf-epicprod HTTPS URL'
# Fetch only main from the fixed URL, never a PR ref or a workflow's ref.
git -C "$REPO" fetch --quiet --no-tags "$CANONICAL" "+refs/heads/main:$REF"
target=$(git -C "$REPO" rev-parse "$REF^{commit}")

if [[ "$(cat "$STATE")" == "$target" ]]; then
    # Also retries a previously failed status POST, without rebuilding anything.
    status success 'Main is deployed (or required no application deploy)'
    exit 0
fi

[[ "$(git -C "$REPO" symbolic-ref --quiet --short HEAD)" == main ]] || refuse 'checkout is not on main'
[[ "$(git -C "$REPO" rev-parse --abbrev-ref --symbolic-full-name '@{upstream}')" == origin/main ]] || \
    refuse 'main must track origin/main'
[[ -z "$(git -C "$REPO" status --porcelain)" ]] || refuse 'checkout has uncommitted changes'
git -C "$REPO" merge-base --is-ancestor HEAD "$target" || refuse 'checkout has commits outside canonical main'
git -C "$REPO" merge-base --is-ancestor "$(cat "$STATE")" "$target" || refuse 'main does not contain the deployed commit'

status pending 'Deploying merged main on swf-testbed'
git -C "$REPO" merge --quiet --ff-only "$target"
[[ "$(git -C "$REPO" rev-parse HEAD)" == "$target" ]] || refuse 'checkout moved during preparation'
log "deploying canonical main $target"
(cd "$REPO" && bash scripts/autodeploy.sh)
# autodeploy.sh may pull a newer main while this poll is running. Accept it
# only when the recorded deployed commit contains the commit we advertised.
git -C "$REPO" merge-base --is-ancestor "$target" "$(cat "$STATE")" || \
    refuse 'deploy script did not record this main commit'
status success 'Main is deployed (or required no application deploy)'
log "completed $target"
