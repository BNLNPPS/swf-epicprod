@echo off
REM Build synrad_service natively on Windows against an installed
REM simphony-core (build-simphony-core.cmd). The Windows counterpart of
REM build-synrad-service.sh.
REM
REM Usage: build-synrad-service.cmd <simphony-src-dir> <simphony-install-dir> <build-dir>

setlocal

if "%~3"=="" (
    echo usage: %~n0 ^<simphony-src-dir^> ^<simphony-install-dir^> ^<build-dir^>
    exit /b 2
)
set "SRC=%~1"
set "PREFIX=%~2"
set "BUILD=%~3"

if not defined CUDA_PATH set "CUDA_PATH=C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.6"
if not defined DEPS_ROOT set "DEPS_ROOT=C:\Users\Shadow\tools\simphony-deps"
if not defined CMAKE_EXE set "CMAKE_EXE=C:\Users\Shadow\tools\cmake\bin\cmake.exe"

call "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat" >nul 2>&1

"%CMAKE_EXE%" -S "%~dp0..\synrad-service" -B "%BUILD%" -G Ninja ^
    -DCMAKE_BUILD_TYPE=Release ^
    -DSYNRAD_GUN_DIR="%SRC%\examples\synrad" ^
    -DSIMPHONY_SRC_DIR="%SRC%" ^
    -DCUDAToolkit_ROOT="%CUDA_PATH%" ^
    -DCMAKE_PREFIX_PATH="%PREFIX%;%DEPS_ROOT%"
if errorlevel 1 (
    echo ERROR: configure failed.
    exit /b 1
)

"%CMAKE_EXE%" --build "%BUILD%" --parallel
if errorlevel 1 (
    echo ERROR: build failed.
    exit /b 1
)
echo built: %BUILD%\synrad_service.exe
