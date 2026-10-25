# 实飞测试一键启动（装桨叶 / 有线耳机 / 语音起飞-跟随 / 全程日志）
#
# 用法（在本目录打开 PowerShell）：
#     .\start_flight_test.ps1              # 默认：离线语音（本地 whisper + SAPI），不需要任何 API Key
#     .\start_flight_test.ps1 -DryRun      # 只做飞行前检查，不启动
#     .\start_flight_test.ps1 -Hybrid      # 换用 hybrid（横移+转向）跟随配置
#     .\start_flight_test.ps1 -Volc        # 豆包全双工（需先 $env:VOLC_API_KEY，且没有图传窗口）
#
# 窗口：本终端 = 窗口1（状态/语音日志），OpenCV = 窗口2（图传 + 跟随 HUD）
# 急停：Ctrl+Shift+Alt+A 悬停 / Ctrl+Shift+Alt+L 降落（全局，任何窗口都有效）；F12/F11 为备选
# 日志：logs\flight_<时间>.log（全程控制台输出） + logs\flight_turns.jsonl（每轮对话记录）

param(
    [string]$Config = 'modules\follow\configs\aircraft_A_ID0.json',
    [int]$MinBattery = 60,
    [switch]$Hybrid,
    [switch]$Volc,
    [switch]$NoVideo,
    [switch]$DryRun,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
$env:PYTHONIOENCODING = 'utf-8'

$py = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { $py = 'python' }

Write-Host '============== 实飞测试 · 飞行前检查 ==============' -ForegroundColor Cyan
Write-Host ("python : {0}" -f $py)
Write-Host ("时间   : {0}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))

# ---- 1) 跟随配置 ----
if ($Hybrid) { $Config = 'modules\follow\configs\offline_candidates\aircraft_A_ID0_hybrid.json' }
$cfgPath = Join-Path $root $Config
if (-not (Test-Path $cfgPath)) {
    Write-Host "[失败] 找不到跟随配置：$cfgPath" -ForegroundColor Red
    exit 1
}
$env:R3_FOLLOW_CONFIG = $cfgPath

# ---- 2) 本机检查（配置 / 语音链路 / 飞机连通与电量）----
$probeArgs = @('tools\flight_preflight.py', '--min-battery', "$MinBattery")
if ($DryRun) { $probeArgs += '--no-drone' }
$lines = @(& $py -X utf8 -B @probeArgs 2>&1)
$values = @{}
foreach ($line in $lines) {
    if ($line -match '^([A-Za-z_]+)=(.*)$') {
        $values[$Matches[1]] = $Matches[2]
        Write-Host ('    ' + $line)
    } else {
        Write-Host ('    ' + $line) -ForegroundColor DarkGray
    }
}

if ($values['CONTROL_PROFILE'] -ne 'openpose_pid') {
    Write-Host '[失败] control_profile 不是 openpose_pid（会跑旧算法），先检查配置文件' -ForegroundColor Red
    exit 1
}
if ($values['STT_CACHED'] -ne 'True') {
    Write-Host '[警告] 本地 whisper 模型未缓存：第一次说话会先下载（需要外网）' -ForegroundColor Yellow
}
if ($values['SAPI_AVAILABLE'] -ne 'True') {
    Write-Host '[警告] Windows SAPI 不可用：语音播报会静音（文字与动作仍正常）' -ForegroundColor Yellow
}

$verdict = $values['VERDICT']
if ($verdict -eq 'block:drone_sdk_stuck') {
    Write-Host '[失败] 飞机在（Wi-Fi 通）但 SDK 命令口不响应：长按电源键关机 → 等 5 秒 → 开机 → 等 20 秒 → 重连 Wi-Fi。' -ForegroundColor Red
    if (-not $Force) { exit 1 }
}
if ($verdict -eq 'block:drone_unreachable' -or $verdict -eq 'block:drone_error') {
    Write-Host '[失败] 连不上飞机（192.168.10.1:8889）：先连 Tello 的 Wi-Fi 再来一次。' -ForegroundColor Red
    if (-not $Force) { exit 1 }
}
if ($verdict -eq 'block:battery_low') {
    Write-Host ("[失败] 电量 {0}% 低于门限 {1}%：Tello 在 50% 左右会直接拒绝 takeoff，先充电。" -f $values['BATTERY'], $MinBattery) -ForegroundColor Red
    if (-not $Force) { exit 1 }
}
if ($values['BATTERY']) {
    Write-Host ("[电量] {0}%（门限 {1}%）" -f $values['BATTERY'], $MinBattery) -ForegroundColor Green
}

# ---- 3) 现场提醒 ----
Write-Host ''
Write-Host '急停（全局，任何窗口都有效）: Ctrl+Shift+Alt+A = 悬停归零   Ctrl+Shift+Alt+L = 立即降落' -ForegroundColor Yellow
Write-Host '备选终端热键                : F12 = 悬停归零   F11 = 立即降落' -ForegroundColor DarkGray
Write-Host '语音口令（离线语音是固定口令，说这几个词最稳）:' -ForegroundColor Yellow
Write-Host '    起飞 / 跟着我 / 停止跟随 / 停在这里 / 降落 / 还有多少电' -ForegroundColor DarkGray
Write-Host ''

# ---- 4) 组装参数 ----
New-Item -ItemType Directory -Force -Path (Join-Path $root 'logs') | Out-Null
$logFile = Join-Path $root ('logs\flight_{0}.log' -f (Get-Date -Format 'yyyyMMdd_HHmmss'))

if ($Volc) {
    if (-not $env:VOLC_API_KEY) {
        Write-Host '[失败] 缺少 VOLC_API_KEY（$env:VOLC_API_KEY = "<密钥>"）' -ForegroundColor Red
        exit 1
    }
    $argv = @('modules\ai\main.py', '--real-drone', '--allow-real-takeoff', '--volc-dialog',
              '--min-battery', "$MinBattery", '--config', 'modules\ai\config.flight-test.json',
              '--log-file', $logFile)
    Write-Host '[模式] 豆包全双工：云端口语理解（此模式不开图传窗口）' -ForegroundColor Cyan
} else {
    $argv = @('modules\ai\main.py', '--real-drone', '--allow-real-takeoff',
              '--offline-voice', '--always-on', '--min-battery', "$MinBattery",
              '--config', 'modules\ai\config.flight-test.json', '--log-file', $logFile)
    if (-not $NoVideo) { $argv += '--show-video' }
    Write-Host '[模式] 离线语音：本地 whisper 识别 + SAPI 播报，不需要 API Key' -ForegroundColor Cyan
}

if ($DryRun) {
    Write-Host ''
    Write-Host '[DryRun] 只检查，不启动。实际命令：' -ForegroundColor Cyan
    Write-Host ('    ' + $py + ' -X utf8 -B ' + ($argv -join ' '))
    exit 0
}

Write-Host ("[日志] {0}" -f $logFile) -ForegroundColor Green
Write-Host '====== 启动：窗口1 = 本终端，窗口2 = 图传（q 关窗口，Ctrl+C 结束）======' -ForegroundColor Cyan
Write-Host ''

# 探测刚占了 Tello 的 UDP 会话，给它 2 秒清掉再让 main.py 正式握手
Start-Sleep -Milliseconds 2000
& $py -X utf8 -B @argv
$code = $LASTEXITCODE
Write-Host ''
Write-Host ("[结束] 退出码 {0}；日志：{1}" -f $code, $logFile) -ForegroundColor Cyan
exit $code
