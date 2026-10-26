<#
.SYNOPSIS
    AI 语音地面测试启动器（桨叶必须拔掉，全程不授权起飞）。

.DESCRIPTION
    启动 modules\ai\main.py 的真机模式，但**故意不传 --allow-real-takeoff**：
    - 语音说"起飞"会被拒绝，飞机永远停在地面；
    - 说"向前走/升高/跟着我"会被底层以"尚未起飞"拦下，只验证链路与语音；
    - 说"还有多少电/现在多高/什么状态"会读真实读数并播报。

    会先跑两遍自检：tools\voice_ground_check.py（语音链路）+ tools\flight_preflight.py（只读读电量）。
    然后弹出终端主控台；加 -Video 才弹图传窗口。

.EXAMPLE
    .\start_voice_ground_test.cmd                     # 直接开测
    .\start_voice_ground_test.cmd -ListDevices        # 先连蓝牙耳机，看设备号
    .\start_voice_ground_test.cmd -InputDevice 5 -OutputDevice 7
    .\start_voice_ground_test.cmd -DryRun             # 只自检，不启动
    .\start_voice_ground_test.cmd -Realtime           # 走四组新的全双工链路（需 AI_REALTIME_* 环境变量）
#>
param(
    [switch]$DryRun,
    [switch]$ListDevices,
    [switch]$MicTest,
    [switch]$Loopback,
    [switch]$NoDrone,
    [int]$InputDevice = -1,
    [int]$OutputDevice = -1,
    [switch]$Video,
    [switch]$Realtime,
    [switch]$Volc,
    [double]$SilenceS = 1.5,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
Set-Location $root
$py = Join-Path $root '.venv\Scripts\python.exe'
$entry = Join-Path $root 'modules\ai\main.py'
$checker = Join-Path $root 'tools\voice_ground_check.py'
$preflight = Join-Path $root 'tools\flight_preflight.py'

Write-Host ''
Write-Host '=================================================================' -ForegroundColor Cyan
if ($NoDrone) {
    Write-Host ' AI 语音地面测试（模拟飞机 · 不连接真机）' -ForegroundColor Cyan
} else {
    Write-Host ' AI 语音地面测试（真机连接 · 不授权起飞 · 桨叶必须拔掉）' -ForegroundColor Cyan
}
Write-Host '=================================================================' -ForegroundColor Cyan

if (-not (Test-Path $py)) {
    Write-Host "[错误] 找不到虚拟环境：$py" -ForegroundColor Red
    Write-Host '  python -m venv .venv' -ForegroundColor Yellow
    Write-Host '  .\.venv\Scripts\python.exe -m pip install -r modules\ai\requirements.txt' -ForegroundColor Yellow
    exit 1
}
if (-not (Test-Path $entry)) {
    Write-Host "[错误] 找不到入口：$entry" -ForegroundColor Red
    exit 1
}
Write-Host "python : $py" -ForegroundColor DarkGray

# ---------- 1. 只看设备 ----------
if ($ListDevices) {
    Write-Host ''
    Write-Host '--- 音频设备（> 默认输入   < 默认输出）---' -ForegroundColor Green
    & $py -X utf8 -B $checker --devices
    Write-Host ''
    Write-Host '连上蓝牙耳机后重跑一次，把它的输入/输出编号传给启动器：' -ForegroundColor Yellow
    Write-Host '  start_voice_ground_test.cmd -InputDevice <输入号> -OutputDevice <输出号>' -ForegroundColor Yellow
    Write-Host '（蓝牙耳机通常同时出现在 MME / DirectSound / WASAPI，优先选 MME 或 WASAPI）' -ForegroundColor DarkGray
    exit 0
}

# ---------- 2. 语音链路自检 ----------
Write-Host ''
Write-Host '[1/3] 语音链路自检' -ForegroundColor Green
$checkArgs = @('tools\voice_ground_check.py')
if ($MicTest) {
    # 真录 3 秒：确认蓝牙耳机麦克风真的能收到声音，而不是"能打开但采不到"
    $checkArgs += '--record-test'
    if ($InputDevice -ge 0) { $checkArgs += @('--input-device', "$InputDevice") }
}
if ($Loopback) {
    # 合成语音→识别→口令映射：不用说话也能证明"理解链路"是通的
    $checkArgs += '--loopback'
}
& $py -X utf8 -B @checkArgs
$checkOk = ($LASTEXITCODE -eq 0)
if (-not $checkOk) {
    Write-Host ''
    Write-Host '[停止] 语音链路有必须解决的项（见上面的 FAIL）。' -ForegroundColor Red
    Write-Host '       修好后重跑；确认只是缺 OpenCV 之类的可选项时，可加 -Force 强开。' -ForegroundColor Yellow
    if (-not $Force) { exit 1 }
    Write-Host '[继续] 已加 -Force，忽略 FAIL 继续启动。' -ForegroundColor Yellow
}

# ---------- 3. 真机只读预检（不碰 8889） ----------
Write-Host ''
Write-Host '[2/3] 真机只读预检（电量/高度/温度，不下发任何指令）' -ForegroundColor Green
# 预检会按 R3_FOLLOW_CONFIG 校验控制档；地面测试不跟随，用标准配置即可
$followCfg = Join-Path $root 'modules\follow\configs\aircraft_A_ID0.json'
if (Test-Path $followCfg) {
    $env:R3_FOLLOW_CONFIG = $followCfg
} else {
    Write-Host "[提示] 找不到 $followCfg，预检的控制档校验会退回默认值" -ForegroundColor DarkGray
}
$preflightOk = $true
if ($NoDrone) {
    Write-Host '[跳过] -NoDrone：模拟飞机模式，不连接真机' -ForegroundColor Yellow
} elseif (Test-Path $preflight) {
    $probeArgs = @('tools\flight_preflight.py', '--min-battery', '10',
                   '--temp-max', '95', '--temp-warn', '85')
    if ($DryRun) { $probeArgs += '--no-drone' }
    & $py -X utf8 -B @probeArgs
    if ($LASTEXITCODE -ne 0) {
        $preflightOk = $false
        Write-Host ''
        Write-Host '[警告] 预检未通过（多半是还没连上 Tello 的 Wi-Fi）。' -ForegroundColor Yellow
        Write-Host '       地面测试不下发动作，可以照样验证语音；要强开请加 -Force。' -ForegroundColor Yellow
        if (-not $Force) {
            Write-Host '[停止] 未加 -Force，先不启动。连好 Wi-Fi 后重跑本脚本即可。' -ForegroundColor Red
            exit 0
        }
        Write-Host '[继续] 已加 -Force，跳过预检结果继续启动。' -ForegroundColor Yellow
    }
} else {
    Write-Host "[跳过] 找不到 $preflight" -ForegroundColor Yellow
}

# ---------- 4. 组装参数（关键：永远不加 --allow-real-takeoff） ----------
$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$logFile = Join-Path $root ("logs\voice_ground_{0}.log" -f $stamp)
$argv = @('modules\ai\main.py')
# -NoDrone：连不上飞机（没电/连不上 Wi-Fi）也要能验语音，此时走模拟飞机
if (-not $NoDrone) { $argv += '--real-drone' }
$argv += @(
    '--min-battery', '10',
    '--config', 'modules\ai\config.flight-test.json',
    '--log-file', $logFile,
    '--silence-s', "$SilenceS"
)
if ($Volc) {
    # 豆包全双工：ASR + 对话 + TTS 都在火山服务端，鉴权是 X-Api-Key
    $argv += '--volc-dialog'
    if (-not $env:VOLC_API_KEY) {
        Write-Host ''
        Write-Host '[停止] -Volc 需要 VOLC_API_KEY（火山控制台「API Key 管理」里的密钥值）：' -ForegroundColor Red
        Write-Host '       $env:VOLC_API_KEY = "<密钥值>"' -ForegroundColor Yellow
        Write-Host '       注意是密钥值本身，不是 api-key-20261024xxxx 这种名称。' -ForegroundColor Yellow
        Write-Host '       只想听本地语音就改用：start_voice_ground_test.cmd（离线 whisper + SAPI）' -ForegroundColor Yellow
        if (-not $Force) { exit 1 }
        Write-Host '[继续] 已加 -Force，仍按 -Volc 启动（预计握手会鉴权失败）。' -ForegroundColor Yellow
    } else {
        Write-Host ('[密钥] VOLC_API_KEY 已设置（长度 {0}）' -f $env:VOLC_API_KEY.Length) -ForegroundColor Green
    }
    if ($env:VOLC_MODEL) { Write-Host ("[模型] {0}" -f $env:VOLC_MODEL) -ForegroundColor DarkGray }
    if ($env:VOLC_VOICE) { Write-Host ("[音色] {0}" -f $env:VOLC_VOICE) -ForegroundColor DarkGray }
    # 豆包要外网，Tello 的 Wi-Fi 没有外网 —— 提前探一下，别等握手超时才发现
    Write-Host '[网络] 检查豆包端点连通性……' -ForegroundColor Cyan
    $cloudOk = $false
    try {
        $cloudOk = Test-NetConnection -ComputerName 'openspeech.bytedance.com' -Port 443 -InformationLevel Quiet -WarningAction SilentlyContinue
    } catch { $cloudOk = $false }
    if ($cloudOk) {
        Write-Host '[网络] openspeech.bytedance.com:443 可达' -ForegroundColor Green
    } else {
        Write-Host '[警告] 连不上豆包端点。豆包需要外网，而 Tello 的 Wi-Fi 没有外网：' -ForegroundColor Yellow
        Write-Host '       请让电脑同时接外网（有线 / 手机热点），否则握手会超时。' -ForegroundColor Yellow
        Write-Host '       只想验语音可改用离线模式：不加 -Volc（本地 whisper + SAPI）。' -ForegroundColor Yellow
    }
} elseif ($Realtime) {
    # 四组新的全双工链路：不走路 no-voice/offline-voice，走 --realtime
    $argv += '--realtime'
    $key = $env:AI_REALTIME_API_KEY
    if (-not $key) {
        Write-Host ''
        Write-Host '[停止] -Realtime 需要这些环境变量：' -ForegroundColor Red
        Write-Host '       $env:AI_REALTIME_API_KEY = "<密钥>"' -ForegroundColor Yellow
        Write-Host '       $env:AI_REALTIME_URL     = "wss://openspeech.bytedance.com/api/v3/duplex/realtime/dialogue"' -ForegroundColor Yellow
        Write-Host '       $env:AI_REALTIME_MODEL   = "1.2.6.0"' -ForegroundColor Yellow
        Write-Host '       $env:AI_REALTIME_VOICE   = "zh_female_vv_jupiter_bigtts"' -ForegroundColor Yellow
        if (-not $Force) { exit 1 }
        Write-Host '[继续] 已加 -Force，仍按 -Realtime 启动（预计会报缺少密钥）。' -ForegroundColor Yellow
    }
} else {
    $argv += @('--offline-voice', '--always-on')
}
if ($Video) { $argv += '--show-video' }
if ($InputDevice -ge 0) { $argv += @('--input-device', "$InputDevice") }
if ($OutputDevice -ge 0) { $argv += @('--output-device', "$OutputDevice") }

# 全双工链路（豆包 / Realtime）拒绝系统默认设备，必须显式给索引；
# 蓝牙耳机在设备列表里的编号会随连接状态漂移，这里按名字自动挑一对。
if (($Volc -or $Realtime) -and $InputDevice -lt 0 -and $OutputDevice -lt 0) {
    Write-Host '[设备] 全双工链路需要显式设备，正在自动挑选蓝牙耳机……' -ForegroundColor Cyan
    $pickOut = @(& $py -X utf8 -B 'tools\voice_ground_check.py' --pick 2>&1)
    $pickedIn = -1
    $pickedOutIdx = -1
    foreach ($line in $pickOut) {
        if ($line -match '^INPUT=(\d+)$') { $pickedIn = [int]$Matches[1] }
        elseif ($line -match '^OUTPUT=(\d+)$') { $pickedOutIdx = [int]$Matches[1] }
        elseif ($line -match '^PICK_ERROR=(.*)$') { Write-Host ("[设备] " + $Matches[1]) -ForegroundColor Yellow }
    }
    if ($pickedIn -ge 0 -and $pickedOutIdx -ge 0) {
        $argv += @('--input-device', "$pickedIn", '--output-device', "$pickedOutIdx")
        Write-Host ("[设备] 自动选择 输入 #{0} / 输出 #{1}" -f $pickedIn, $pickedOutIdx) -ForegroundColor Green
    } else {
        Write-Host '[停止] 没找到蓝牙耳机：先连上耳机，或用 -InputDevice / -OutputDevice 手工指定。' -ForegroundColor Red
        Write-Host '       离线模式（不加 -Volc）可以用笔记本麦克风，不用指定设备。' -ForegroundColor Yellow
        if (-not $Force) { exit 1 }
        Write-Host '[继续] 已加 -Force，仍继续启动（预计会因缺少设备失败）。' -ForegroundColor Yellow
    }
}

if ($argv -contains '--allow-real-takeoff') {
    Write-Host '[严重] 参数里出现了 --allow-real-takeoff，已强制移除（地面测试不允许起飞）' -ForegroundColor Red
    $argv = $argv | Where-Object { $_ -ne '--allow-real-takeoff' }
}

# ---------- 5. 开测说明 ----------
Write-Host ''
Write-Host '[3/3] 准备就绪' -ForegroundColor Green
Write-Host ''
Write-Host '安全兜底（任何窗口聚焦都有效）：' -ForegroundColor Cyan
Write-Host '  Ctrl+Shift+Alt+A = 零速度悬停      Ctrl+Shift+Alt+L = 立即降落' -ForegroundColor Cyan
Write-Host '  终端备用热键 F12 / F11 同样有效' -ForegroundColor DarkGray
Write-Host ''
Write-Host '建议按这个顺序说（每句说完停一下，等它播报完再说下一句）：' -ForegroundColor Cyan
Write-Host '  1) 还有多少电        → 读电量并播报'
Write-Host '  2) 现在什么状态      → 打印电量/高度/链路/是否在飞'
Write-Host '  3) 现在多高          → 读高度'
if ($NoDrone) {
    Write-Host '  4) 起飞              → 模拟飞机上会"起飞"成功（结果标记 simulated）'
    Write-Host '  5) 向前走30厘米      → 模拟飞机上会正常执行（结果标记 simulated）'
} else {
    Write-Host '  4) 起飞              → 应被拒绝：[安全] 真机起飞被拒绝（地面测试未授权）'
    Write-Host '  5) 向前走30厘米      → 应被底层拦下：尚未起飞 / is_landed'
}
Write-Host '  6) 急停              → 走优先通道，立即零速度'
Write-Host '  7) 结束              → 关闭免按键监听'
Write-Host ''
Write-Host '语音模式：' -NoNewline -ForegroundColor Cyan
if ($Volc) { Write-Host '豆包全双工实时语音（火山 Seeduplex，ASR+对话+TTS 均在服务端）' }
elseif ($Realtime) { Write-Host '四组全双工实时链路（兼容 Realtime API）' }
else { Write-Host '离线：本地 whisper 识别 + Windows SAPI 播报（不需 API Key）' }
Write-Host '麦克风/播放：' -NoNewline -ForegroundColor Cyan
if ($InputDevice -ge 0 -or $OutputDevice -ge 0) {
    Write-Host ("输入 #{0}  输出 #{1}" -f $InputDevice, $OutputDevice)
} else {
    Write-Host '系统默认设备（要指定蓝牙耳机请加 -InputDevice / -OutputDevice）'
}
Write-Host "日志文件：$logFile" -ForegroundColor DarkGray
Write-Host ''

$cmdline = $py + ' -X utf8 -B ' + ($argv -join ' ')
if ($DryRun) {
    Write-Host '[DryRun] 只检查，不启动。实际命令：' -ForegroundColor Yellow
    Write-Host $cmdline
    exit 0
}

Write-Host '启动中……（控制台里可以用 s 急停 / q 退出 等单键操作）' -ForegroundColor Green
& $py -X utf8 -B @argv
$code = $LASTEXITCODE
Write-Host ''
Write-Host ("[结束] 退出码 {0}；日志在 {1}" -f $code, $logFile) -ForegroundColor DarkGray
exit $code
