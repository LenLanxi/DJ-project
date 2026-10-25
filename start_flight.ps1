# 实飞一键启动：豆包全双工语音 + 三组 openpose_pid 跟随（hybrid 横移/转向）+ 全局急停热键
#
# 用法（在本目录打开 PowerShell）：
#     $env:VOLC_API_KEY = "<你的豆包密钥>"     # 只需设一次，仅存在当前窗口
#     .\start_flight.ps1
#
# 急停：F12 = 零速度悬停 / F11 = 立即降落（全局热键，终端在后台也有效；Ctrl+C 只是收尾不降落）

param(
    [string]$Config = 'modules\follow\configs\offline_candidates\aircraft_A_ID0_hybrid.json',
    [int]$MinBattery = 60,
    [switch]$NoVideo,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$py = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { $py = 'python' }
$env:PYTHONIOENCODING = 'utf-8'

Write-Host '================ 实飞启动检查 ================' -ForegroundColor Cyan

# ---- 1) 跟随配置：必须显式指定，否则退回 legacy（旧算法，无横移）----
$cfgPath = Join-Path $root $Config
if (-not (Test-Path $cfgPath)) {
    Write-Host "[失败] 找不到跟随配置：$cfgPath" -ForegroundColor Red
    exit 1
}
$env:R3_FOLLOW_CONFIG = $cfgPath

Push-Location (Join-Path $root 'modules\follow')
try {
    $dump = & $py -X utf8 -B -c @'
from r3_follow.config import load_config
c = load_config()
print('control_profile=' + c.control_profile)
print('steering_mode=' + c.steering_mode)
print('target_id=' + str(c.target_id))
print('instance_id=' + c.instance_id)
print('dictionary=' + c.dictionary)
'@
} finally {
    Pop-Location
}
Write-Host '[跟随配置] R3_FOLLOW_CONFIG =' $cfgPath -ForegroundColor Green
$dump | ForEach-Object { Write-Host ('           ' + $_) }

$dumpText = ($dump -join "`n")
if ($dumpText -notmatch 'control_profile=openpose_pid') {
    Write-Host '[失败] control_profile 不是 openpose_pid —— 会跑旧算法，先检查配置文件' -ForegroundColor Red
    exit 1
}

# ---- 2) 豆包密钥：只从环境变量读，脚本里绝不写死 ----
if (-not $env:VOLC_API_KEY) {
    Write-Host ''
    Write-Host '[失败] 缺少 VOLC_API_KEY（脚本不保存密钥，需你先在窗口里设置）' -ForegroundColor Red
    Write-Host '       $env:VOLC_API_KEY = "<火山控制台 API Key 管理里的密钥值>"' -ForegroundColor Yellow
    Write-Host '       注意：api-key-2026xxxx 那串是"名称"，真正要用的是 UUID 形式的密钥值' -ForegroundColor DarkGray
    exit 1
}
Write-Host ('[豆包密钥] 已设置（长度 {0}，前 4 位 {1}****）' -f $env:VOLC_API_KEY.Length, $env:VOLC_API_KEY.Substring(0, [Math]::Min(4, $env:VOLC_API_KEY.Length))) -ForegroundColor Green

# ---- 3) 起飞门限：Tello 固件在电量约 50% 时会直接拒绝起飞 ----
Write-Host ("[电量门限] 低于 {0}% 拒绝起飞" -f $MinBattery) -ForegroundColor Green
Write-Host '[急停] F12 = 零速度悬停   F11 = 立即降落（全局热键，无需切回终端）' -ForegroundColor Yellow

# ---- 4) 起飞参数 ----
$argv = @(
    'modules\ai\main.py',
    '--real-drone',
    '--allow-real-takeoff',
    '--volc-dialog',
    '--min-battery', $MinBattery
)
if (-not $NoVideo) { $argv += '--show-video' }

if ($DryRun) {
    Write-Host ''
    Write-Host '[DryRun] 仅检查，不启动：' -ForegroundColor Cyan
    Write-Host ('         ' + $py + ' ' + ($argv -join ' '))
    exit 0
}

Write-Host ''
Write-Host '================ 启动 ================' -ForegroundColor Cyan
Write-Host '语音可这样说：起飞 / 开始跟随 / 停止跟随 / 降落' -ForegroundColor DarkGray
Write-Host ''
& $py -X utf8 -B @argv
