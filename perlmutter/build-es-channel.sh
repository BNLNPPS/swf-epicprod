#!/bin/bash
# Build yampl, the messaging library the pilot's Event Service executor hands
# event ranges to the payload through, for the Perlmutter pilot: yampl v1.0
# and its Python binding python-yampl (the pair the pilot's event-service code
# speaks; tools/npps0/build-yampl.sh says why v1.0), compiled inside
# the same ALRB AlmaLinux 9 container and against the same ALRB Python
# the pilot runs under at NERSC (docs/NERSC_PERLMUTTER.md), so the
# binding loads there. The result is a relocatable tarball,
#
#   es-channel-<python>-el9.tar.gz
#     es-channel/lib/libyampl.so
#     es-channel/python/yampl.cpython-*.so   (rpath $ORIGIN/../lib)
#
# which the launcher unpacks into the pilot's working directory and
# puts on PYTHONPATH (ES_CHANNEL_URL). Publication is the bucket's
# pilot prefix (docs/DEVCLOUD_STAGEOUT.md, section 4).
#
#   build-es-channel.sh [build dir] [campaign image] [--resume]
#
# Without an image, use the Perlmutter ALRB Python on AlmaLinux 9. With
# an image, compile against its Python and libraries through Apptainer.
# Campaign builds require a new build directory unless --resume is supplied.
#
# No set -e/-u here: ALRB's setup, sourced below, is not clean under
# either; the build itself runs under both, inside the container.

BUILD=${1:-/data/swf-tmp/es-channel-build}
IMAGE=${2:-}
RESUME=${3:-}
if [[ -n "$RESUME" && "$RESUME" != --resume ]]; then
    echo "unknown build option: $RESUME" >&2
    exit 1
fi
if [[ -n "$IMAGE" && -e "$BUILD" && "$RESUME" != --resume ]]; then
    echo "campaign build directory already exists: $BUILD" >&2
    exit 1
fi
mkdir -p "$BUILD" || exit 1
BUILD=$(cd "$BUILD" && pwd)

# The build, run inside the container with $BUILD as /srv.
# lsetup is ALRB's shell function, so the Python is set up in ALRB's
# shell and the build body runs under -eu in a child of it.
printf '%s\n' 'lsetup -q "python pilot-default-SL9"' 'bash -eu /srv/build-body.sh' > "$BUILD/inside.sh"
cat > "$BUILD/build-body.sh" <<'EOF'
cd /srv
PY=$(which python3)
PYTAG=$($PY -c 'import sys; print(f"py{sys.version_info[0]}{sys.version_info[1]}")')
PLATFORM=el9
if [[ ${EPICPROD_CHANNEL_CAMPAIGN:-0} == 1 ]]; then
    source /etc/os-release
    PLATFORM="$ID${VERSION_ID%%.*}"
    # Campaign images expose Spack development files under /opt/local.
    export CPATH="/opt/local/include${CPATH:+:$CPATH}"
    export LIBRARY_PATH="/opt/local/lib${LIBRARY_PATH:+:$LIBRARY_PATH}"
fi
echo "python $PY ($PYTAG)"
OUT=/srv/es-channel
if [[ ${EPICPROD_CHANNEL_RESUME:-0} != 1 ]]; then
    rm -rf "$OUT" /srv/pytools
fi
mkdir -p "$OUT/lib" "$OUT/python" "$OUT/include"
$PY -m pip install -q --target /srv/pytools setuptools cython
export PYTHONPATH=/srv/pytools

[[ -d yampl-1.0 ]] || git clone -q --branch v1.0 https://github.com/vitillo/yampl yampl-1.0
cd yampl-1.0
./configure --prefix="$OUT" > configure.log 2>&1
make -j8 libyampl.so > make.log 2>&1
cp libyampl.so "$OUT/lib/"
mkdir -p "$OUT/lib/pkgconfig"
cp yampl.pc "$OUT/lib/pkgconfig/"
cp -R include/yampl "$OUT/include/"

cd /srv
[[ -d python-yampl ]] || git clone -q https://github.com/vitillo/python-yampl
cd python-yampl
if [[ ${EPICPROD_CHANNEL_RESUME:-0} != 1 ]]; then
    rm -rf build yampl.cpp
fi
# setup.py assumes pkg-config terminates its flags with a space and newline.
# Parse both implementations' output without dropping a path or library letter.
$PY - <<'PYSETUP'
from pathlib import Path
p = Path('setup.py')
text = p.read_text()
text = text.replace('.decode("utf-8")[2:-2]', '.decode("utf-8").strip()[2:]')
text = text.replace('.decode("utf-8")[:-2]', '.decode("utf-8").split()')
text = text.replace('extra_link_args=[yampl_libs]', 'extra_link_args=yampl_libs')
p.write_text(text)
PYSETUP
export PKG_CONFIG_PATH=$OUT/lib/pkgconfig
export CPATH="$OUT/include/yampl:$OUT/include${CPATH:+:$CPATH}"
export LIBRARY_PATH="$OUT/lib${LIBRARY_PATH:+:$LIBRARY_PATH}"
export LDFLAGS='-Wl,-rpath,$ORIGIN/../lib'
$PY setup.py build_ext --inplace > build.log 2>&1
cp yampl.cpython-*.so "$OUT/python/"
rm -rf "$OUT/include" "$OUT/lib/pkgconfig"

# The pilot's pattern over the 'local' (shared memory) context, from a
# copy elsewhere, so the rpath and not the build tree resolves the library.
rm -rf /srv/check && mkdir -p /srv/check && cp -R "$OUT" /srv/check/
cd /tmp
PYTHONPATH=/srv/check/es-channel/python $PY - <<'PYEOF'
import yampl
s = yampl.ServerSocket("build_es_channel_check", "local")
size, buf = s.try_recv_raw()
assert size == -1, (size, buf)
print("python-yampl ok:", yampl.__file__)
PYEOF
cd /srv
tar -czf "es-channel-$PYTAG-$PLATFORM.tar.gz" es-channel
sha256sum "es-channel-$PYTAG-$PLATFORM.tar.gz"
EOF

if [[ -n "$IMAGE" ]]; then
    apptainer exec --cleanenv --bind "$BUILD:/srv" --pwd /srv \
        --env "EPICPROD_CHANNEL_CAMPAIGN=1,EPICPROD_CHANNEL_RESUME=$([[ $RESUME == --resume ]] && echo 1 || echo 0)" "$IMAGE" \
        bash -eu /srv/build-body.sh || exit $?
    ls -l "$BUILD"/es-channel-*.tar.gz || exit 1
    exit 0
fi

export ATLAS_LOCAL_ROOT_BASE=/cvmfs/atlas.cern.ch/repo/ATLASLocalRootBase
export ALRB_CONT_CHOME=$BUILD/.alrb
export ALRB_CONT_RUNPAYLOAD="source /srv/inside.sh"
mkdir -p "$ALRB_CONT_CHOME"
cd "$BUILD"
( source "$ATLAS_LOCAL_ROOT_BASE/user/atlasLocalSetup.sh" -c el9 -q )
ls -l "$BUILD"/es-channel-*-el9.tar.gz || { echo "build failed: no tarball" >&2; exit 1; }
