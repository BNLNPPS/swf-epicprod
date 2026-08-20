@echo off
REM Build the Simphony GPU core packages (SysRap, CSG, QUDArap, CSGOptiX)
REM natively on Windows: the coprocessor worker of VOLUNTEER_GPU_PLAN.md.
REM The Windows counterpart of build-simphony-core.sh.
REM
REM Usage: build-simphony-core.cmd <simphony-src-dir> <build-dir> <install-dir>
REM
REM Recipe notes, each paid for during bring-up on shadow-pc:
REM - No Geant4 exists on this platform, so package selection must happen at
REM   configure time. The Linux script configures the whole tree and then
REM   builds one target; that works only because the container supplies
REM   Geant4. Here SIMPHONY_CORE_ONLY skips u4, g4cx and src outright.
REM - vcvars64 rebuilds PATH, so the CUDA bin directory must be prepended
REM   after it runs, never before: cmd expands %PATH% when the line is
REM   parsed, so a prepend chained ahead of vcvars silently loses cl.exe.
REM - glm is a hard dependency of sysrap (sysrap/CMakeLists.txt), header-only
REM   but required through its CMake config package.
REM - CUDA 12.6 accepts MSVC 19.44: its host_config.h gate is
REM   _MSC_VER < 1910 || _MSC_VER >= 1950.
REM - Do not let the CUDA installer place a display driver on a cloud
REM   streaming host; install with an explicit component list.

setlocal

if "%~3"=="" (
    echo usage: %~n0 ^<simphony-src-dir^> ^<build-dir^> ^<install-dir^>
    exit /b 2
)
set "SRC=%~1"
set "BUILD=%~2"
set "PREFIX=%~3"

REM Toolchain locations, all overridable from the environment.
if not defined CUDA_PATH set "CUDA_PATH=C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.6"
REM glm, nlohmann_json and plog, placed by install-windows-deps.ps1.
if not defined DEPS_ROOT set "DEPS_ROOT=C:\Users\Shadow\tools\simphony-deps"
if not defined CMAKE_EXE set "CMAKE_EXE=C:\Users\Shadow\tools\cmake\bin\cmake.exe"
REM RTX A4500 is Ampere GA102.
if not defined CUDA_ARCH set "CUDA_ARCH=86"

set "VSWHERE_DIR=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer"
if not exist "%VSWHERE_DIR%\vswhere.exe" (
    echo ERROR: vswhere not found in "%VSWHERE_DIR%"; install VS 2022 Build Tools.
    exit /b 1
)
REM Run vswhere from its own directory via an explicit relative path: a quoted
REM absolute path as the first token of a for /f command is mis-parsed by cmd,
REM and a bare name is not necessarily resolved against the current directory.
pushd "%VSWHERE_DIR%"
for /f "usebackq delims=" %%i in (`.\vswhere.exe -products * -latest -property installationPath`) do set "VSDIR=%%i"
popd
if not defined VSDIR (
    echo ERROR: no Visual Studio installation found.
    exit /b 1
)

REM vcvars64.bat prints "'vswhere.exe' is not recognized" to stderr from its
REM own vsdevcmd chain and then succeeds. The message is Microsoft's, not
REM this script's, and the resulting environment is correct.
call "%VSDIR%\VC\Auxiliary\Build\vcvars64.bat" >nul
if errorlevel 1 (
    echo ERROR: vcvars64.bat failed.
    exit /b 1
)

REM After vcvars, never before.
set "PATH=%CUDA_PATH%\bin;%PATH%"

if not exist "%CUDA_PATH%\bin\nvcc.exe" (
    echo ERROR: nvcc not found under "%CUDA_PATH%".
    exit /b 1
)

"%CMAKE_EXE%" -S "%SRC%" -B "%BUILD%" -G Ninja ^
    -DCMAKE_BUILD_TYPE=Release ^
    -DSIMPHONY_CORE_ONLY=ON ^
    -DBUILD_TESTING=OFF ^
    -DCMAKE_CUDA_ARCHITECTURES=%CUDA_ARCH% ^
    -DCUDAToolkit_ROOT="%CUDA_PATH%" ^
    -DCMAKE_PREFIX_PATH="%DEPS_ROOT%" ^
    -DCMAKE_INSTALL_PREFIX="%PREFIX%"
if errorlevel 1 (
    echo ERROR: cmake configure failed.
    exit /b 1
)

"%CMAKE_EXE%" --build "%BUILD%" --parallel
if errorlevel 1 (
    echo ERROR: cmake build failed.
    exit /b 1
)

"%CMAKE_EXE%" --install "%BUILD%"
if errorlevel 1 (
    echo ERROR: cmake install failed.
    exit /b 1
)

echo built and installed: %PREFIX%
endlocal
