#!/bin/bash
# Build a canary pilot tarball: the released pilot3 tarball from the
# production pilot directory on CVMFS plus the files a branch of the
# pilot3 clone changes against the release tag. The result is
# pilot3-<version>-epic<N>.tar.gz with the release's top-level pilot3/
# layout and its PILOTVERSION unchanged; the name carries the patch.
#
#   build-canary-pilot.sh <version> <N> [pilot3 tree] [build dir]
#   build-canary-pilot.sh 3.14.3.3 2
#
# The tree's HEAD must be the branch to ship, and <version> must be a tag
# in it. Every file that differs between the release tarball and the
# result is listed; the build stops if a file is deleted or added rather
# than changed. Publication to the canary directory is the
# cvmfs-canary-publish procedure (docs/OSG_SUBMISSION.md); a local trial
# on the test queue is PILOTURL=local with the tarball as
# ~/npps0-config/pilot3.tar.gz (docs/NPPS0_TEST_QUEUE.md).

set -eu

VERSION=${1:?version}
N=${2:?patch number}
TREE=${3:-/data/wenauseic/github/pilot3}
BUILD=${4:-/data/swf-tmp/pilot-build}
PROD=/cvmfs/eic.opensciencegrid.org/panda/pilot
RELEASE=$PROD/pilot3-$VERSION.tar.gz
OUT=$BUILD/pilot3-$VERSION-epic$N.tar.gz

[[ -f "$RELEASE" ]] || { echo "ERROR: no release tarball $RELEASE" >&2; exit 1; }
git -C "$TREE" rev-parse -q --verify "refs/tags/$VERSION" > /dev/null || { echo "ERROR: tag $VERSION not in $TREE" >&2; exit 1; }
if [[ -n "$(git -C "$TREE" status --porcelain)" ]]; then
    echo "ERROR: $TREE has uncommitted changes; commit or stash them first" >&2
    exit 1
fi

mkdir -p "$BUILD"
rm -rf "$BUILD/rel" "$BUILD/new"
mkdir -p "$BUILD/rel" "$BUILD/new"
tar -xzf "$RELEASE" -C "$BUILD/rel"
tar -xzf "$RELEASE" -C "$BUILD/new"
[[ -d "$BUILD/new/pilot3" ]] || { echo "ERROR: release tarball has no top-level pilot3/" >&2; exit 1; }

echo "branch: $(git -C "$TREE" branch --show-current) at $(git -C "$TREE" rev-parse --short HEAD)"
echo "files changed against $VERSION:"
git -C "$TREE" diff --name-status "$VERSION"..HEAD | while read -r status path; do
    echo "  $status $path"
    case "$status" in
        M) cp "$TREE/$path" "$BUILD/new/pilot3/$path" ;;
        *) echo "ERROR: only modified files are carried; $status $path" >&2; exit 1 ;;
    esac
done

echo "difference between the release and the result:"
diff -rq "$BUILD/rel/pilot3" "$BUILD/new/pilot3" | sed 's/^/  /'

echo "PILOTVERSION: $(cat "$BUILD/new/pilot3/PILOTVERSION")"
(cd "$BUILD/new" && tar -czf "$OUT" pilot3)
sha256sum "$OUT"
