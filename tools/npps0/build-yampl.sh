#!/bin/bash
# Build the Event Service channel library for the pilot on npps0:
# yampl v1.0 (the C++ library, with its bundled ZeroMQ) and python-yampl
# (the Cython binding whose API the pilot speaks: send_raw, and
# try_recv_raw returning (size, bytes)), installed under ~/yampl-1.0
# for the pilot's python, /usr/local/bin/python3. The pass script puts
# ~/yampl-1.0/python on PYTHONPATH; the module carries an rpath to the
# library, so nothing else is set.
#
# Why v1.0 and not yampl master: the master branch's pybind11 binding
# returns bytes rather than (size, bytes) from try_recv_raw and
# segfaults in tryRecv on this host (2026-09-09); v1.0 with python-yampl
# is the pair the pilot's event-service code was written against.
#
# Run on npps0 as wenaus. Needs gcc, make, git and pkg-config; installs
# setuptools and cython for the pilot's python with pip --user. The
# library's own tests do not build on this glibc (a bare wait() call),
# so only the library target is built and installed by hand, as the
# Makefile's install target would.

set -eu

PY=/usr/local/bin/python3
PREFIX=$HOME/yampl-1.0
SRC=$HOME/src

export PATH=$HOME/.local/bin:$PATH
$PY -m pip install --user -q setuptools cython

mkdir -p "$SRC"
cd "$SRC"
[[ -d yampl-1.0 ]] || git clone -q --branch v1.0 https://github.com/vitillo/yampl yampl-1.0
cd yampl-1.0
./configure --prefix="$PREFIX" > configure.log 2>&1
make -j8 libyampl.so > make.log 2>&1
mkdir -p "$PREFIX/lib/pkgconfig" "$PREFIX/include"
cp libyampl.so "$PREFIX/lib/"
cp yampl.pc "$PREFIX/lib/pkgconfig/"
cp -R include/yampl "$PREFIX/include/"

cd "$SRC"
[[ -d python-yampl ]] || git clone -q https://github.com/vitillo/python-yampl
cd python-yampl
rm -rf build yampl.cpp
export PKG_CONFIG_PATH=$PREFIX/lib/pkgconfig
export CPATH=$PREFIX/include            # SocketFactory.h includes yampl/ISocketFactory.h
export LIBRARY_PATH=$PREFIX/lib         # setup.py passes -L and -l as one malformed argument
export LDFLAGS="-Wl,-rpath,$PREFIX/lib"
$PY setup.py build_ext --inplace > build.log 2>&1
mkdir -p "$PREFIX/python"
cp yampl.cpython-*.so "$PREFIX/python/"

# The pilot's pattern over the 'local' (shared memory) context.
cd /tmp
PYTHONPATH=$PREFIX/python $PY - <<'EOF'
import yampl
s = yampl.ServerSocket("build_yampl_check", "local")
size, buf = s.try_recv_raw()
assert size == -1, (size, buf)
print("python-yampl ok:", yampl.__file__)
EOF
