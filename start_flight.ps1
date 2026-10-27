# One production launcher. uv owns the project environment; no pip or silent fallback.
# No argument means real voice control with takeoff blocked until explicitly authorized.
# .\start_flight.ps1 -AllowTakeoff
# .\start_flight.ps1 -DryRun                 inspect settings, no network/hardware/API
param(
    [string]$Config = 'modules\follow\configs\aircraft_A_ID0_motion_follow.json',
    [string]$Address = '',
    [int]$MinBattery = 30,
    [int]$MaxSessionSeconds = 180,
    [ValidateSet('land', 'hover')][string]$ExitAction = 'land',
    [string]$Program = '',
    [string]$Text = '',
    [switch]$PlanOnly,
    [switch]$AllowTakeoff,
    [switch]$ShowVideo,
    [switch]$DryRun
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $projectRoot
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw 'uv is required. Install uv first; this launcher never installs into global Python.'
}
if ($Program -and $Text) { throw 'Choose either -Program or -Text.' }
if ($PlanOnly -and -not $Text) { throw '-PlanOnly requires -Text.' }
if ($MinBattery -lt 20 -or $MinBattery -gt 100) { throw '-MinBattery must be 20..100.' }
if ($MaxSessionSeconds -lt 1 -or $MaxSessionSeconds -gt 900) { throw '-MaxSessionSeconds must be 1..900.' }
if (-not $Program -and -not $Text -and $MaxSessionSeconds -gt 180) {
    throw 'RTC voice -MaxSessionSeconds must be 1..180, including DryRun.'
}

$configPath = if ([IO.Path]::IsPathRooted($Config)) { $Config } else { Join-Path $projectRoot $Config }
$configPath = (Resolve-Path -LiteralPath $configPath).Path
$followConfig = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($followConfig.control_profile -ne 'openpose_pid') {
    throw 'Production requires an explicit openpose_pid configuration. Follow source is unchanged.'
}
$env:R3_FOLLOW_CONFIG = $configPath
Write-Host ('Follow config: {0}' -f $configPath)
Write-Host ('Profile: {0}; steering: {1}; marker ID: {2}' -f $followConfig.control_profile, $followConfig.steering_mode, $followConfig.target_id)
Write-Host ('Takeoff authorized: {0}; min battery: {1}%; exit action: {2}' -f $AllowTakeoff.IsPresent, $MinBattery, $ExitAction)
Write-Host 'Emergency: Ctrl+Shift+Alt+A = stop; Ctrl+Shift+Alt+L = land; F12/F11 are alternatives.'
Write-Host 'One process controls one Tello. Three ordinary Tellos do not become a swarm from three config files.'

$runtimeArgs = @('modules\ai\main.py', '--follow-config', $configPath,
    '--min-battery', "$MinBattery", '--max-session-seconds', "$MaxSessionSeconds", '--exit-action', $ExitAction)
if (-not $PlanOnly) { $runtimeArgs += '--real-drone' }
if ($AllowTakeoff) { $runtimeArgs += '--allow-real-takeoff' }
if ($Address) { $runtimeArgs += @('--address', $Address) }
if ($Program) { $runtimeArgs += @('--program', $Program) }
if ($Text) { $runtimeArgs += @('--text', $Text) }
if ($PlanOnly) { $runtimeArgs += '--plan-only' }
if ($ShowVideo -and -not $PlanOnly) { $runtimeArgs += '--show-video' }
if ($DryRun) {
    Write-Host 'DryRun: configuration only. No aircraft, audio stream, Docker or API is opened.'
    Write-Host ('uv run --frozen --python 3.11 python -X utf8 -B ' + ($runtimeArgs -join ' '))
    exit 0
}
if (-not $Text -and -not $Program) {
    $required = @('RTC_APP_ID','RTC_APP_KEY','VOLCENGINE_ACCESS_KEY_ID','VOLCENGINE_SECRET_ACCESS_KEY',
        'VOLC_S2S_APP_ID','VOLC_S2S_TOKEN','RTC_VLM_MODEL')
    $missing = @($required | Where-Object { -not [Environment]::GetEnvironmentVariable($_) })
    if ($missing.Count) { throw ('RTC configuration missing: ' + ($missing -join ', ')) }
    Write-Host 'Open the printed local URL manually and select the Bluetooth microphone and speaker in the browser.'
}
& uv run --frozen --python 3.11 python -X utf8 -B @runtimeArgs
exit $LASTEXITCODE
