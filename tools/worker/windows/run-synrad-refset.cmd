@echo off
REM Run the Windows synrad_service against the synchrotron-radiation
REM reference set and judge the hits statistically. The Windows counterpart
REM of the replay+compare stages of run-synrad-containment.sh: the input
REM photon array (not the gun) is the cross-platform contract.
REM
REM Usage: run-synrad-refset.cmd <service-exe> <refset-dir> <simphony-src-dir> <trial-dir>

setlocal

if "%~4"=="" (
    echo usage: %~n0 ^<service-exe^> ^<refset-dir^> ^<simphony-src-dir^> ^<trial-dir^>
    exit /b 2
)
set "EXE=%~1"
set "REFSET=%~2"
set "SRC=%~3"
set "TRIAL=%~4"

if not defined PYTHON_EXE set "PYTHON_EXE=C:\Users\Shadow\AppData\Local\Programs\Python\Python312\python.exe"
REM The simphony exported targets link shared cudart, so cudart64_12.dll must
REM be resolvable; a deployed worker ships the redistributable DLL beside the
REM exe instead.
if not defined CUDA_PATH set "CUDA_PATH=C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.6"
set "PATH=%CUDA_PATH%\bin;%PATH%"

if not exist "%TRIAL%" mkdir "%TRIAL%"

REM Geometry resolution (spath::CFBaseFromGEOM): GEOM names the geometry,
REM <GEOM>_CFBaseFromGEOM points at the directory holding CSGFoundry\.
set "GEOM=synrad"
set "synrad_CFBaseFromGEOM=%REFSET%\geometry"
set "OPTICKS_MAX_SLOT=600000"

cd /d "%TRIAL%"

echo == replay: synrad_service file-fed from the refset inphoton array ==
"%EXE%" -i "%REFSET%\inphoton\synrad_service_inphoton.npy" -o "%TRIAL%"
REM exact comparison, not "if errorlevel 1": hard crashes (missing DLL,
REM access violation) return NEGATIVE codes that errorlevel 1 lets through
if not "%errorlevel%"=="0" (
    echo ERROR: synrad_service failed with exit %errorlevel%.
    exit /b 1
)

echo == judge: Windows hits vs the Linux service reference ==
"%PYTHON_EXE%" "%SRC%\optiphy\ana\synrad_test.py" ^
    "%TRIAL%\synrad_service_hits.npy" "%REFSET%\hits-service\synrad_service_hits.npy" --nphoton 500000
if errorlevel 1 (
    echo ERROR: statistical comparison FAILED.
    exit /b 1
)
echo PASS
