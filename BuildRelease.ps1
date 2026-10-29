<#
.SYNOPSIS
Build a portable Windows x64 directory and ZIP. Build-time uv/npm are required;
the resulting application needs neither uv, npm, Docker nor installed Python/Node.
.DESCRIPTION
Run only after the source review and Check.ps1 pass. Downloads Node from nodejs.org,
uses an existing uv-managed CPython 3.11, and installs only locked production packages.
SelfCheck is offline and never connects an aircraft or a cloud account.
The output directory must not exist; this script never overwrites an earlier release.
#>
[CmdletBinding()]
param(
    [string]$OutputDirectory = '',
    [string]$NodeVersion = '24.14.1'
)
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
$projectRoot = $PSScriptRoot
if (-not [Environment]::Is64BitOperatingSystem -or $env:PROCESSOR_ARCHITECTURE -ne 'AMD64') {
    throw 'Build on Windows x64.'
}
if ($NodeVersion -notmatch '^\d+\.\d+\.\d+$') { throw 'Invalid Node version.' }
foreach ($tool in @('uv', 'npm', 'git')) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) { throw "$tool is required on the build machine." }
}
if (-not $OutputDirectory) { $OutputDirectory = Join-Path $projectRoot ('dist\CompanionWindows' + (Get-Date -Format yyyyMMddTHHmmss)) }
$OutputDirectory = [IO.Path]::GetFullPath($OutputDirectory)
if ((Test-Path -LiteralPath $OutputDirectory) -or (Test-Path -LiteralPath ($OutputDirectory + '.zip'))) {
    throw 'Output already exists; choose a new directory.'
}
$taskFolder = Join-Path ([IO.Path]::GetTempPath()) ('CompanionBuild' + [guid]::NewGuid().ToString('N'))
$previousEnvironment = $env:UV_PROJECT_ENVIRONMENT
$previousPath = $env:PATH
New-Item -ItemType Directory -Path $taskFolder, $OutputDirectory | Out-Null
try {
    Push-Location -LiteralPath $projectRoot
    try {
        $pythonBase = & uv run --frozen --no-sync python -c 'import sys; print(sys.base_prefix)'
        if ($LASTEXITCODE -ne 0) { throw 'Cannot locate project Python.' }
        $pythonBase = [string]($pythonBase | Select-Object -Last 1)
        $managedDirectory = & uv python dir
        if ($LASTEXITCODE -ne 0 -or -not $pythonBase.StartsWith(([string]$managedDirectory).TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
            throw 'The project must use a uv-managed standalone CPython; system Python is not portable.'
        }
        $pythonExecutable = Join-Path $pythonBase 'python.exe'
        $identity = & $pythonExecutable -I -c 'import sys,struct,json;print(json.dumps([sys.version_info[:2],struct.calcsize(chr(80))]))' | ConvertFrom-Json
        if ($identity[0][0] -ne 3 -or $identity[0][1] -ne 11 -or $identity[1] -ne 8) { throw 'Python 3.11 x64 required.' }
        $env:UV_PROJECT_ENVIRONMENT = Join-Path $taskFolder 'Environment'
        & uv sync --frozen --no-dev --no-install-project --python $pythonExecutable
        if ($LASTEXITCODE -ne 0) { throw 'Locked production dependency installation failed.' }
        $pythonTarget = Join-Path $OutputDirectory 'Python'
        Copy-Item -LiteralPath $pythonBase -Destination $pythonTarget -Recurse
        $bundledScripts = [IO.Path]::GetFullPath((Join-Path $pythonTarget 'Scripts'))
        if (-not $bundledScripts.StartsWith($OutputDirectory + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Scripts path outside release.' }
        if (Test-Path -LiteralPath $bundledScripts) { Remove-Item -LiteralPath $bundledScripts -Recurse -Force }
        $packageTarget = [IO.Path]::GetFullPath((Join-Path $pythonTarget 'Lib\site-packages'))
        if (-not $packageTarget.StartsWith($OutputDirectory + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Package path outside release.' }
        # Replace the standalone interpreter's bundled build tools with only the locked runtime closure.
        if (Test-Path -LiteralPath $packageTarget) { Remove-Item -LiteralPath $packageTarget -Recurse -Force }
        New-Item -ItemType Directory -Path $packageTarget | Out-Null
        Copy-Item -Path (Join-Path $env:UV_PROJECT_ENVIRONMENT 'Lib\site-packages\*') -Destination $packageTarget -Recurse -Force

        foreach ($folder in @('app', 'drone_sdk', 'execution', 'providers', 'navigation', 'follow')) {
            foreach ($source in Get-ChildItem -LiteralPath (Join-Path $projectRoot $folder) -Recurse -File) {
                $relative = $source.FullName.Substring($projectRoot.Length + 1)
                if ($relative -match '(^|\\)(__pycache__|logs|node_modules)(\\|$)') { continue }
                if ($source.Extension -ne '.py' -and $relative -notlike 'follow\configs\*.json' -and $source.Name -ne 'LICENSE') { continue }
                $target = Join-Path $OutputDirectory $relative
                New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
                Copy-Item -LiteralPath $source.FullName -Destination $target
            }
        }
        foreach ($name in @('pyproject.toml', 'uv.lock', 'start_flight.ps1')) {
            Copy-Item -LiteralPath (Join-Path $projectRoot $name) -Destination $OutputDirectory
        }
        $webTarget = Join-Path $OutputDirectory 'web'
        New-Item -ItemType Directory -Path $webTarget | Out-Null
        Get-ChildItem -LiteralPath (Join-Path $projectRoot 'web') -File | Where-Object {
            $_.Extension -in @('.js', '.html') -or $_.Name -in @('package.json', 'package-lock.json', 'License.txt', 'Provenance.txt')
        } | Copy-Item -Destination $webTarget
        & npm --prefix $webTarget ci --omit=dev --ignore-scripts --no-audit --no-fund
        if ($LASTEXITCODE -ne 0) { throw 'Locked Node dependency installation failed.' }
        $nodeArchive = Join-Path $taskFolder 'Node.zip'
        Invoke-WebRequest -Uri "https://nodejs.org/dist/v$NodeVersion/node-v$NodeVersion-win-x64.zip" -OutFile $nodeArchive
        Expand-Archive -LiteralPath $nodeArchive -DestinationPath (Join-Path $taskFolder 'Node')
        $nodeSource = Join-Path $taskFolder "Node\node-v$NodeVersion-win-x64"
        $nodeTarget = Join-Path $OutputDirectory 'Node'
        New-Item -ItemType Directory -Path $nodeTarget | Out-Null
        foreach ($name in @('node.exe', 'LICENSE')) { Copy-Item -LiteralPath (Join-Path $nodeSource $name) -Destination $nodeTarget }

        @'
param([string]$Credentials = '', [switch]$AllowTakeoff, [switch]$RecordTest, [switch]$SelfCheck)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:PATH = (Join-Path $PSScriptRoot 'Node') + ';' + $env:PATH
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$python = Join-Path $PSScriptRoot 'Python\python.exe'
if ($SelfCheck) {
    & $python -E -s -X utf8 -B (Join-Path $PSScriptRoot 'SelfCheck.py')
    exit $LASTEXITCODE
}
if (-not $Credentials) { $Credentials = Join-Path $PSScriptRoot 'Credentials.json' }
if (-not (Test-Path -LiteralPath $Credentials -PathType Leaf)) { throw 'Copy CredentialsExample.json to Credentials.json and enter your own account settings first.' }
$arguments = @('-E', '-s', '-X', 'utf8', '-B', '-m', 'app.Application', 'voice', '--real-drone', '--doubao-credentials', $Credentials,
    '--follow-config', (Join-Path $PSScriptRoot 'follow\configs\aircraft_A_ID0_motion_follow.json'))
if ($AllowTakeoff) { $arguments += '--allow-real-takeoff' }
if ($RecordTest) { $arguments += '--record-test' }
Write-Host 'Open http://127.0.0.1:18848 manually when ready. F12 stops; F11 lands. Ctrl+C exits with landing requested.'
& $python @arguments
exit $LASTEXITCODE
'@ | Set-Content -LiteralPath (Join-Path $OutputDirectory 'Launch.ps1') -Encoding UTF8
        @'
"""Offline package verification; never opens hardware, cloud or credentials."""
import importlib
import json
from pathlib import Path
import subprocess
import sys
import socket
root = Path(__file__).resolve().parent
sys.path.insert(0, str(root))
# Imports and validation must not quietly make a cloud or aircraft connection.
class OfflineSocket(socket.socket):
    def __init__(self, *args, **kwargs):
        raise AssertionError('SelfCheck attempted network access')
socket.socket = OfflineSocket
for name in ('numpy','cv2','av','websockets','pynput','djitellopy','volcenginesdkarkruntime',
             'app.Application','app.Runtime','execution.Host','providers.RTCBridge','providers.RTCRuntime'):
    importlib.import_module(name)
from app.Tools import validate_action, validate_tool
validate_action({'action':'get_status','args':[]})
from follow.r3_follow.config import load_config
from app.Application import make_parser
config_path = root / 'follow' / 'configs' / 'aircraft_A_ID0_motion_follow.json'
config = load_config(config_path)
assert config.target_id == 0
arguments = make_parser().parse_args(['voice', '--real-drone', '--follow-config', str(config_path),
                                      '--doubao-credentials', str(root/'CredentialsExample.json')])
assert arguments.command == 'voice' and not arguments.allow_real_takeoff
try:
    validate_tool('run_program', {'code':'print(1)'})
except ValueError:
    pass
else:
    raise AssertionError('Old code execution is still exposed')
node = root / 'Node' / 'node.exe'
assert sys.version_info[:2] == (3, 11)
assert Path(sys.base_prefix).resolve() == (root / 'Python').resolve(), 'Interpreter escaped the package'
from importlib.metadata import distributions
packages = sorted((d.metadata['Name'], d.version) for d in distributions())
assert not any(name.lower() in ('pytest', 'pip', 'setuptools') for name, _ in packages), 'Build/development packages leaked'
subprocess.run([str(node), '--check', 'RTCService.js'], cwd=root/'web', check=True)
subprocess.run([str(node), '-e', "require('node:net').Socket.prototype.connect=function(){throw Error('Offline SelfCheck attempted network')};require('@volcengine/openapi');require.resolve('@volcengine/rtc');require('./RTCClient.js');console.log('Node dependencies OK')"], cwd=root/'web', check=True)
print(json.dumps({'ok':True,'python':sys.version.split()[0],'platform':sys.platform,'packages':len(packages),'scope':'offline imports and structured contract only'}))
'@ | Set-Content -LiteralPath (Join-Path $OutputDirectory 'SelfCheck.py') -Encoding UTF8
        '{"RTC":{"AppId":"","AppKey":""},"OpenAPI":{"AccessKeyId":"","SecretAccessKey":""},"Speech":{"AppId":"","AccessToken":""},"Ark":{"ModelName":"","APIKey":""}}' | Set-Content -LiteralPath (Join-Path $OutputDirectory 'CredentialsExample.json') -Encoding UTF8
        @'
Windows 11 x64 portable package. Extract fully into a writable local directory.
No Python, Node, uv or Docker installation is needed. Use existing Edge/Chrome.
Run: powershell -ExecutionPolicy Bypass -File .\Launch.ps1 -SelfCheck
Copy CredentialsExample.json to Credentials.json and enter your own cloud settings.
Start: powershell -ExecutionPolicy Bypass -File .\Launch.ps1
Takeoff is blocked unless -AllowTakeoff is supplied. Optional -RecordTest retains media.
Open http://127.0.0.1:18848 manually and select audio devices. Start microphone explicitly.
F12 stops; F11 lands. Ctrl+C requests landing and shutdown. Verify physical aircraft state.
Offline SelfCheck does not certify cloud connectivity, microphone, camera or real flight.
VC++ x64 runtime may be needed on clean Windows. N/KN editions may need Media Feature Pack.
Third-party license notices are retained with Python, Node and their dependency packages.
Do not share Credentials.json, logs or Recordings. Recordings are retained until you delete them.
'@ | Set-Content -LiteralPath (Join-Path $OutputDirectory 'Readme.txt') -Encoding UTF8
        $commit = & git rev-parse HEAD
        $dirty = [bool](& git status --porcelain)
        @{ SourceCommit = [string]$commit; SourceDirty = $dirty; NodeVersion = $NodeVersion; BuiltAt = [DateTime]::UtcNow.ToString('o') } |
            ConvertTo-Json | Set-Content -LiteralPath (Join-Path $OutputDirectory 'Build.json') -Encoding UTF8
        # Remove caches before verifying; package neither build tools nor the temporary venv.
        Get-ChildItem -LiteralPath $OutputDirectory -Directory -Filter __pycache__ -Recurse | ForEach-Object {
            $resolved = [IO.Path]::GetFullPath($_.FullName)
            if (-not $resolved.StartsWith($OutputDirectory + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Cache path outside release.' }
            Remove-Item -LiteralPath $resolved -Recurse -Force
        }
        $env:PATH = (Join-Path $env:SystemRoot 'System32') + ';' + $env:SystemRoot
        & (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe') -NoProfile -ExecutionPolicy Bypass -File (Join-Path $OutputDirectory 'Launch.ps1') -SelfCheck
        if ($LASTEXITCODE -ne 0) { throw 'Isolated PATH package SelfCheck failed. No ZIP was created.' }
        Compress-Archive -Path (Join-Path $OutputDirectory '*') -DestinationPath ($OutputDirectory + '.zip') -CompressionLevel Optimal
        Write-Host ('Portable directory: ' + $OutputDirectory)
        Write-Host ('Archive: ' + $OutputDirectory + '.zip')
    } finally { Pop-Location }
} finally {
    $env:UV_PROJECT_ENVIRONMENT = $previousEnvironment
    $env:PATH = $previousPath
    $resolvedTask = [IO.Path]::GetFullPath($taskFolder)
    $temporaryRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
    if (-not $resolvedTask.StartsWith($temporaryRoot, [StringComparison]::OrdinalIgnoreCase)) { throw 'Temporary directory outside TEMP.' }
    if (Test-Path -LiteralPath $resolvedTask) { Remove-Item -LiteralPath $resolvedTask -Recurse -Force }
}
