#!/bin/bash
# Build the Event Service channel library for the Perlmutter pilot:
# yampl v1.0 and python-yampl (the pair the pilot's event-service code
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
#   build-es-channel.sh [build dir]     on a host with ALRB and apptainer
#
# No set -e/-u here: ALRB's setup, sourced below, is not clean under
# either; the build itself runs under both, inside the container.

BUILD=${1:-/data/swf-tmp/es-channel-build}
mkdir -p "$BUILD"
BUILD=$(cd "$BUILD" && pwd)

# The build, run inside the container with $BUILD as /srv.
# lsetup is ALRB's shell function, so the Python is set up in ALRB's
# shell and the build body runs under -eu in a child of it.
printf '%s\n' 'lsetup -q "python pilot-default-SL9"' 'bash -eu /srv/build-body.sh' > "$BUILD/inside.sh"
cat > "$BUILD/build-body.sh" <<'EOF'
cd /srv
PY=$(which python3)
PYTAG=$($PY -c 'import sys; print(f"py{sys.version_info[0]}{sys.version_info[1]}")')
echo "python $PY ($PYTAG)"
OUT=/srv/es-channel
rm -rf "$OUT" /srv/pytools
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
rm -rf build yampl.cpp
export PKG_CONFIG_PATH=$OUT/lib/pkgconfig
export CPATH=$OUT/include
export LIBRARY_PATH=$OUT/lib
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
tar -czf "es-channel-$PYTAG-el9.tar.gz" es-channel
sha256sum "es-channel-$PYTAG-el9.tar.gz"
EOF

export ATLAS_LOCAL_ROOT_BASE=/cvmfs/atlas.cern.ch/repo/ATLASLocalRootBase
export ALRB_CONT_CHOME=$BUILD/.alrb
export ALRB_CONT_RUNPAYLOAD="source /srv/inside.sh"
mkdir -p "$ALRB_CONT_CHOME"
cd "$BUILD"
( source "$ATLAS_LOCAL_ROOT_BASE/user/atlasLocalSetup.sh" -c el9 -q )
ls -l "$BUILD"/es-channel-*-el9.tar.gz || { echo "build failed: no tarball" >&2; exit 1; }
