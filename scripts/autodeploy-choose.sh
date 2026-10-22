#!/usr/bin/env bash
# autodeploy-choose.sh: the deploy a set of changed swf-epicprod paths needs
# (docs/AUTODEPLOY.md). Reads paths on stdin, one per line; prints one word:
#
#   none   nothing the swf-monitor deploy ships (docs, tests, scripts and
#          tools run from the dev tree, pilot/Perlmutter/GKE material)
#   ui     lightweight --ui: pcs web code, swf_epicprod analytics/assessment
#   mcp    lightweight --ui --mcp: swf_epicprod/mcp_tools
#   full   full deploy: everything else the package ships, including the
#          payload and every module the ops agent imports
#   stop   a pcs migration or model change: no deploy without explicit approval of the migration
#
# A path matching no rule counts as full. Prints `static` as a second word
# when a static asset changed under a lightweight deploy. Pure: no git, no
# host access, so the PR check runs it on GitHub's own machines.
set -euo pipefail

level=0   # 0 none, 1 ui, 2 mcp, 3 full
stop=""
static=""
not_shipped=()

raise() { (( $1 > level )) && level=$1 || true; }

while IFS= read -r p; do
    [[ -z "$p" ]] && continue
    case "$p" in
        pcs/migrations/*|pcs/models.py)
            stop+="$p " ;;
        pcs/management/*)
            raise 3 ;;
        pcs/static/*|swf_epicprod/*/static/*)
            raise 1; static=static ;;
        pcs/*)
            raise 1 ;;
        swf_epicprod/mcp_tools/*)
            raise 2 ;;
        swf_epicprod/analytics/*|swf_epicprod/assessment/*)
            raise 1 ;;
        swf_epicprod/*|pyproject.toml)
            raise 3 ;;
        docs/*|*.md|LICENSE|.github/*|tests/*|*.ipynb|*.svg)
            ;;
        scripts/*|tools/*|pilot/*|perlmutter/*|gke/*|pandaserver/schedconfig-overrides.json)
            not_shipped+=("$p") ;;
        *)
            raise 3 ;;
    esac
done

if [[ -n "$stop" ]]; then
    echo "stop"
    echo "migration or model change: $stop" >&2
    exit 0
fi
case $level in
    0) echo "none" ;;
    1) echo "ui${static:+ $static}" ;;
    2) echo "mcp${static:+ $static}" ;;
    3) echo "full" ;;
esac
if (( ${#not_shipped[@]} )); then
    echo "not shipped by the swf-monitor deploy (run from the dev tree or deployed by their own procedure): ${not_shipped[*]}" >&2
fi
