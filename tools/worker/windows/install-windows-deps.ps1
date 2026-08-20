<#
Install the header-only third-party dependencies the Simphony core packages
require, into one prefix that the build passes as CMAKE_PREFIX_PATH.

  install-windows-deps.ps1 [-Prefix C:\Users\Shadow\tools\simphony-deps]

sysrap/CMakeLists.txt requires nlohmann_json, plog and glm through their CMake
config packages. GLEW and glfw3 are deliberately absent: they are needed only
for the header-only SGLFW*/SGLM* visualisation code, which no core library
source compiles, and the core-only build sets SIMPHONY_WITH_VIZ=OFF.

Each project is cloned at a pinned tag and installed with its own CMake
install step, which is what generates the config package (none of the three
ship a usable one in the source tree).
#>
[CmdletBinding()]
param(
    [string]$Prefix   = 'C:\Users\Shadow\tools\simphony-deps',
    [string]$WorkDir  = 'C:\Users\Shadow\tools\deps-src',
    [string]$CMakeExe = 'C:\Users\Shadow\tools\cmake\bin\cmake.exe',
    [string]$GitExe   = 'C:\Users\Shadow\tools\PortableGit\cmd\git.exe'
)

# Not 'Stop': Windows PowerShell wraps a native command's stderr in an
# ErrorRecord, so a terminating preference turns git's informational
# detached-HEAD note into a fatal error. Every native call below is checked
# through $LASTEXITCODE instead.
$ErrorActionPreference = 'Continue'

foreach ($exe in @($CMakeExe, $GitExe)) {
    if (-not (Test-Path $exe)) { throw "required tool not found: $exe" }
}

$vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
if (-not (Test-Path $vswhere)) { throw "vswhere not found: install VS 2022 Build Tools" }
$vsdir = & $vswhere -products * -latest -format value -property installationPath
if (-not $vsdir) { throw "no Visual Studio installation found" }
$vcvars = Join-Path $vsdir 'VC\Auxiliary\Build\vcvars64.bat'
if (-not (Test-Path $vcvars)) { throw "vcvars64.bat not found under $vsdir" }

# name, repo, tag, extra cmake args
$deps = @(
    @{ Name = 'glm'
       Repo = 'https://github.com/g-truc/glm.git'
       Tag  = '1.0.3'
       Args = @('-DGLM_BUILD_LIBRARY=OFF', '-DGLM_BUILD_TESTS=OFF', '-DGLM_BUILD_INSTALL=ON')
       Config = 'glmConfig.cmake' },
    @{ Name = 'nlohmann_json'
       Repo = 'https://github.com/nlohmann/json.git'
       Tag  = 'v3.12.0'
       Args = @('-DJSON_BuildTests=OFF')
       Config = 'nlohmann_jsonConfig.cmake' },
    # plog 1.1.10 declares cmake_minimum_required(VERSION 3.0); CMake 4 removed
    # compatibility below 3.5, so the policy floor has to be supplied here.
    @{ Name = 'plog'
       Repo = 'https://github.com/SergiusTheBest/plog.git'
       Tag  = '1.1.10'
       Args = @('-DPLOG_BUILD_SAMPLES=OFF', '-DPLOG_BUILD_TESTS=OFF',
                '-DCMAKE_POLICY_VERSION_MINIMUM=3.5')
       Config = 'plogConfig.cmake' }
)

New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null

foreach ($d in $deps) {
    $src = Join-Path $WorkDir $d.Name
    $bld = Join-Path $WorkDir "$($d.Name)-build"

    if (Test-Path $src) { Remove-Item $src -Recurse -Force }
    if (Test-Path $bld) { Remove-Item $bld -Recurse -Force }

    Write-Host "=== $($d.Name) $($d.Tag) ==="
    & $GitExe clone --quiet --depth 1 --branch $d.Tag $d.Repo $src
    if ($LASTEXITCODE -ne 0) { throw "$($d.Name): git clone failed ($LASTEXITCODE)" }

    # vcvars must be established in the same cmd invocation as the build.
    $cfg = @("`"$CMakeExe`"", '-S', "`"$src`"", '-B', "`"$bld`"", '-G', 'Ninja',
             '-DCMAKE_BUILD_TYPE=Release', "-DCMAKE_INSTALL_PREFIX=`"$Prefix`"") + $d.Args
    cmd /c "`"$vcvars`" >nul && $($cfg -join ' ')"
    if ($LASTEXITCODE -ne 0) { throw "$($d.Name): cmake configure failed ($LASTEXITCODE)" }

    cmd /c "`"$vcvars`" >nul && `"$CMakeExe`" --install `"$bld`""
    if ($LASTEXITCODE -ne 0) { throw "$($d.Name): cmake install failed ($LASTEXITCODE)" }

    $found = Get-ChildItem $Prefix -Recurse -Filter $d.Config -ErrorAction SilentlyContinue
    if (-not $found) { throw "$($d.Name): installed but $($d.Config) not found under $Prefix" }
    Write-Host "    config package: $($found[0].FullName)"
}

Write-Host ""
Write-Host "all dependencies installed under: $Prefix"
Write-Host "pass to the build as -DCMAKE_PREFIX_PATH=$Prefix"
