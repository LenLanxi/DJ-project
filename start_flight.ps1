# One production launcher. uv owns the project environment; no pip or silent fallback.
# No argument means real voice control with takeoff blocked until explicitly authorized.
# .\start_flight.ps1 -AllowTakeoff
# .\start_flight.ps1 -DryRun                 inspect settings, no network/hardware/API
param(
    [string]$Config = 'modules\follow\configs\aircraft_A_ID0_motion_follow.json',
    [string]$Address = '',
    [int]$MinBattery = 30,
    [int]$MaxSessionSeconds = 30000,
    [ValidateRange(1,65535)][int]$WebPort = 18848,
    [ValidateSet('alibaba', 'doubao')][string]$VoiceProvider = 'alibaba',
    [string]$AlibabaCredentials = '',
    [string]$DoubaoCredentials = '',
    [double]$ApiBudgetRmb = 15,
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
if ($MinBattery -lt 0 -or $MinBattery -gt 100) { throw '-MinBattery must be 0..100; zero disables the project percentage threshold only.' }
if ($MaxSessionSeconds -lt 1 -or $MaxSessionSeconds -gt 30000) { throw '-MaxSessionSeconds must be 1..30000.' }
if ([double]::IsNaN($ApiBudgetRmb) -or [double]::IsInfinity($ApiBudgetRmb) -or $ApiBudgetRmb -lt 0 -or $ApiBudgetRmb -gt 15) {
    throw '-ApiBudgetRmb must be 0..15. Zero does not authorize paid API use.'
}
if (-not $AlibabaCredentials) {
    $AlibabaCredentials = if ($env:ALIBABA_CREDENTIALS_FILE) { $env:ALIBABA_CREDENTIALS_FILE } else {
        Join-Path $HOME 'Desktop\tmp\DomesticAiTrials\AlibabaCredentials.json'
    }
}
if (-not $DoubaoCredentials) {
    $DoubaoCredentials = if ($env:DOUBAO_CREDENTIALS_FILE) { $env:DOUBAO_CREDENTIALS_FILE } else {
        Join-Path $HOME 'Desktop\tmp\DoubaoRealtime\Credentials.json'
    }
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
    '--min-battery', "$MinBattery", '--max-session-seconds', "$MaxSessionSeconds", '--exit-action', $ExitAction,
    '--web-port', "$WebPort")
if (-not $PlanOnly) { $runtimeArgs += '--real-drone' }
if ($AllowTakeoff) { $runtimeArgs += '--allow-real-takeoff' }
if ($Address) { $runtimeArgs += @('--address', $Address) }
if ($Program) { $runtimeArgs += @('--program', $Program) }
if ($Text) { $runtimeArgs += @('--text', $Text) }
if ($PlanOnly) { $runtimeArgs += '--plan-only' }
if ($ShowVideo -and -not $PlanOnly) { $runtimeArgs += '--show-video' }
if (-not $Text -and -not $Program) {
    $runtimeArgs += @('--voice-provider', $VoiceProvider, '--alibaba-credentials', $AlibabaCredentials,
        '--doubao-credentials', $DoubaoCredentials, '--api-budget-rmb', "$ApiBudgetRmb")
    Write-Host ('Voice provider: {0}; explicit alternative: -VoiceProvider doubao; no mid-flight provider switch.' -f $VoiceProvider)
    Write-Host ('Local page: http://127.0.0.1:{0}; occupied ports cause an error instead of selecting another port.' -f $WebPort)
    Write-Host ('This run paid API authorization: {0} RMB; local caps do not prove account balance or final billing.' -f $ApiBudgetRmb)
}
if ($DryRun) {
    Write-Host 'DryRun: configuration only. No aircraft, audio stream, Docker or API is opened.'
    Write-Host ('uv run --frozen --python 3.11 python -X utf8 -B ' + ($runtimeArgs -join ' '))
    exit 0
}
if (-not $Text -and -not $Program) {
    Write-Host 'Provider and Ark credentials are checked before aircraft connection. File paths, not keys, are passed to Python.'
    Write-Host 'Open the printed local URL manually and select the Bluetooth microphone and speaker in the browser.'
}
& uv run --frozen --python 3.11 python -X utf8 -B @runtimeArgs
exit $LASTEXITCODE
