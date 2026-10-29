# One production launcher. uv owns the project environment; no pip or silent fallback.
# No argument means real voice control with takeoff blocked until explicitly authorized.
# .\start_flight.ps1 -AllowTakeoff
# .\start_flight.ps1 -DryRun                 inspect settings, no network/hardware/API
param(
    [string]$Config = 'follow\configs\aircraft_A_ID0_motion_follow.json',
    [string]$Address = '',
    [int]$MinBattery = 30,
    [int]$MaxSessionSeconds = 30000,
    [int]$WebPort = 18848,
    [string]$DoubaoCredentials = '',
    [string]$ExitAction = 'land',
    [string]$ActionFile = '',
    [string]$Text = '',
    [switch]$PlanOnly,
    [switch]$AllowTakeoff,
    [switch]$ShowVideo,
    [switch]$RecordTest,
    [switch]$DryRun
)
$ErrorActionPreference = 'Stop'
if ($ActionFile -and $Text) { throw 'ActionFile and Text are mutually exclusive.' }
if ($PlanOnly -and (-not $Text -or $ActionFile)) { throw 'PlanOnly requires Text and cannot use ActionFile.' }
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $projectRoot
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw 'uv is required. Install uv first; this launcher never installs into global Python.'
}
if (-not $DoubaoCredentials) {
    $DoubaoCredentials = if ($env:DOUBAO_CREDENTIALS_FILE) { $env:DOUBAO_CREDENTIALS_FILE } else {
        Join-Path $HOME 'Desktop\tmp\DoubaoRealtime\Credentials.json'
    }
}
$configPath = if ([IO.Path]::IsPathRooted($Config)) { $Config } else { Join-Path $projectRoot $Config }
# Python owns argument and configuration validation for every entry path.
$configPath = [IO.Path]::GetFullPath($configPath)
Write-Host ('Follow config: {0}' -f $configPath)
Write-Host ('Takeoff authorized: {0}; min battery: {1}%; exit action: {2}' -f $AllowTakeoff.IsPresent, $MinBattery, $ExitAction)
Write-Host 'Emergency: Ctrl+Shift+Alt+A = stop; Ctrl+Shift+Alt+L = land; F12/F11 are alternatives.'
Write-Host 'One process controls one Tello.'

$mode = if ($PlanOnly) {'plan'} elseif ($ActionFile -or $Text) {'run'} else {'voice'}
$runtimeArgs = @('-m', 'app.Application', $mode, '--doubao-credentials', $DoubaoCredentials)
if (-not $PlanOnly) { $runtimeArgs += @('--follow-config', $configPath,
    '--min-battery', "$MinBattery", '--exit-action', $ExitAction) }
if (-not $PlanOnly) { $runtimeArgs += '--real-drone' }
if ($AllowTakeoff -and -not $PlanOnly) { $runtimeArgs += '--allow-real-takeoff' }
if ($Address -and -not $PlanOnly) { $runtimeArgs += @('--address', $Address) }
if ($ActionFile) { $runtimeArgs += @('--action-file', $ActionFile) }
if ($Text) { $runtimeArgs += @('--text', $Text) }

if ($ShowVideo -and -not $PlanOnly) { $runtimeArgs += '--show-video' }
if ($mode -eq 'voice') {
    $runtimeArgs += @('--max-session-seconds', "$MaxSessionSeconds", '--web-port', "$WebPort")
    Write-Host 'Voice provider: Doubao'
    Write-Host ('Local page: http://127.0.0.1:{0}; occupied ports cause an error instead of selecting another port.' -f $WebPort)
}
if ($RecordTest) {
    if ($mode -ne 'voice') { throw 'RecordTest is available in voice mode only.' }
    $runtimeArgs += '--record-test'
}
if ($DryRun) {
    Write-Host 'DryRun: command preview only; Python validates arguments when run. No resources are opened.'
    Write-Host ('uv run --frozen --python 3.11 python -X utf8 -B ' + ($runtimeArgs -join ' '))
    exit 0
}
if ($mode -eq 'voice') {
    Write-Host 'Doubao credentials are checked before aircraft connection. File paths, not keys, are passed to Python.'
    Write-Host 'Open the printed local URL manually and select the Bluetooth microphone and speaker in the browser.'
}
& uv run --frozen --python 3.11 python -X utf8 -B @runtimeArgs
exit $LASTEXITCODE
