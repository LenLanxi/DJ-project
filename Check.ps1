<#
.SYNOPSIS
Run offline Python and frontend regressions from any working directory.
.EXAMPLE
.\Check.ps1
.EXAMPLE
.\Check.ps1 -Docker
#>
[CmdletBinding()]
param([switch]$Docker)

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
$previousDocker = $env:RUN_DOCKER_TESTS
$previousUtf8 = $env:PYTHONUTF8
$previousEncoding = $env:PYTHONIOENCODING
$pythonExit = 1
$frontendExit = 1

Push-Location -LiteralPath $PSScriptRoot
try {
    $env:RUN_DOCKER_TESTS = if ($Docker) { '1' } else { '0' }
    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    & uv run --frozen python -B -m pytest -q -o faulthandler_timeout=90
    $pythonExit = $LASTEXITCODE
    & npm --prefix web test
    $frontendExit = $LASTEXITCODE
}
finally {
    $env:RUN_DOCKER_TESTS = $previousDocker
    $env:PYTHONUTF8 = $previousUtf8
    $env:PYTHONIOENCODING = $previousEncoding
    Pop-Location
}

if ($pythonExit -ne 0 -or $frontendExit -ne 0) { exit 1 }
exit 0
