#!/usr/bin/env bash
# autodeploy.sh: deploy what has landed on swf-epicprod main since the last
# deployed commit (docs/AUTODEPLOY.md). Run on swf-testbed by the self-hosted
# GitHub Actions runner on every push to main; safe to run by hand.
#
# Pulls the dev tree, takes the changed paths since the recorded deployed
# commit, asks autodeploy-choose.sh which deploy they need, refuses when a
# tree the deploy ships from is dirty or unpushed, deploys, checks every
# changed package file against the deployed copy, and records the commit.
# Any refusal or failure exits nonzero, so the run shows red in Actions.
set -euo pipefail

GH=/data/wenauseic/github
REPO=$GH/swf-epicprod
MONITOR=$GH/swf-monitor
SHARED=/opt/swf-monitor/shared
STATE=$SHARED/epicprod-deployed-commit
LOG=$SHARED/logs/epicprod-autodeploy.log
# Trees the deploys ship from: lightweight syncs swf-monitor, swf-epicprod and
# snapper-ai from the dev trees; the full deploy freezes swf-epicprod,
# snapper-ai, site-canary and swf-common-lib into the release.
TREES=(swf-epicprod swf-monitor snapper-ai site-canary swf-common-lib)

log() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }
summary() { [[ -n "${GITHUB_STEP_SUMMARY:-}" ]] && echo "$*" >> "$GITHUB_STEP_SUMMARY" || true; }
fail() { log "REFUSED: $*"; summary "**Not deployed:** $*"; exit 1; }

exec 9>"$SHARED/epicprod-autodeploy.lock"
flock 9

[[ -s "$STATE" ]] || fail "no deployed-commit record at $STATE"
last=$(<"$STATE")

[[ -z "$(git -C "$REPO" status --porcelain)" ]] || \
    fail "swf-epicprod dev tree has uncommitted changes; a session is mid-edit"
git -C "$REPO" pull -q --ff-only || fail "swf-epicprod pull --ff-only failed"
head=$(git -C "$REPO" rev-parse HEAD)

if [[ "$head" == "$last" ]]; then
    log "nothing to deploy: $head already deployed"
    summary "Nothing to deploy: \`${head:0:7}\` is already live."
    exit 0
fi

changed=$(git -C "$REPO" diff --name-only "$last" "$head")
verdict=$(echo "$changed" | bash "$REPO/scripts/autodeploy-choose.sh")
kind=${verdict%% *}
log "range ${last:0:7}..${head:0:7}: $verdict ($(echo "$changed" | wc -l) paths)"

if [[ "$kind" == stop ]]; then
    fail "the range changes pcs models or migrations; a migration deploys only by hand, on explicit approval"
fi
if [[ "$kind" == none ]]; then
    echo "$head" > "$STATE"
    log "no deploy needed; recorded ${head:0:7}"
    summary "No deploy needed for \`${last:0:7}..${head:0:7}\` (docs, tests or files not shipped by the deploy)."
    exit 0
fi

for t in "${TREES[@]}"; do
    [[ -z "$(git -C "$GH/$t" status --porcelain)" ]] || fail "$t has uncommitted changes"
    ahead=$(git -C "$GH/$t" rev-list --count '@{u}..HEAD' 2>/dev/null || echo 0)
    [[ "$ahead" == 0 ]] || fail "$t has $ahead unpushed commit(s)"
done

case "$kind" in
    ui|mcp)
        flags=(--ui)
        [[ "$kind" == mcp ]] && flags+=(--mcp)
        [[ "$verdict" == *static* ]] && flags+=(--static)
        log "lightweight deploy ${flags[*]}"
        sudo -n "$MONITOR/deploy-lightweight-ui-mcp.sh" "${flags[@]}" 2>&1 | tee -a "$LOG" \
            || fail "lightweight deploy exited nonzero"
        ;;
    full)
        branch=$(git -C "$MONITOR" branch --show-current)
        log "full deploy, swf-monitor branch $branch"
        sudo -n bash "$MONITOR/deploy-swf-monitor.sh" branch "$branch" 2>&1 | tee -a "$LOG" \
            || fail "full deploy exited nonzero"
        ;;
esac

# The deployed copy of every changed package file must match the dev tree.
py=/opt/swf-monitor/current/.venv/bin/python
pcs_dir=$($py -c "import pcs, os; print(os.path.dirname(pcs.__file__))")
ep_dir=$($py -c "import swf_epicprod, os; print(os.path.dirname(swf_epicprod.__file__))")
mismatch=()
while IFS= read -r p; do
    case "$p" in
        pcs/*/tests/*|swf_epicprod/*/tests/*|pcs/migrations/*) continue ;;
        pcs/*) target=$pcs_dir/${p#pcs/} ;;
        swf_epicprod/*) target=$ep_dir/${p#swf_epicprod/} ;;
        *) continue ;;
    esac
    cmp -s "$REPO/$p" "$target" || mismatch+=("$p")
done < <(git -C "$REPO" diff --name-only --diff-filter=d "$last" "$head")
(( ${#mismatch[@]} == 0 )) || fail "deployed copy differs from main for: ${mismatch[*]}"

status=$(curl -k -s -o /dev/null -w '%{http_code}' https://pandaserver02.sdcc.bnl.gov/swf-monitor/api/ || echo 000)
[[ "$status" == 200 ]] || fail "monitor health check answered HTTP $status after the deploy"

echo "$head" > "$STATE"
log "deployed ${last:0:7}..${head:0:7} ($kind); files verified; health 200"
summary "**Deployed** \`${last:0:7}..${head:0:7}\` by a **$kind** deploy; changed files verified in the live release."
