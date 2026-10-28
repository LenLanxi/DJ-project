# AI 随行小伙伴

面向 Tello 的原型：跟随佩戴标记的用户，接收自然语言，再通过完整 Python 程序执行临时任务并恢复跟随。目标场景包含红色帐篷导航与旋转，但尚未完成整段真机验收；需求来源为 [Companion 原始页面](REQUIREMENTS.html/Companion(1).html)。

## 安装与启动

需要 Windows、uv 管理的 Python 3.11、Node.js 22+、Docker Desktop Linux containers。电脑须同时连接 Tello Wi-Fi 与互联网；先按 [Setup](docs/Setup.md) 准备所选路线的服务端凭据。在仓库根目录执行：

```powershell
uv sync --frozen
npm --prefix web ci --ignore-scripts --no-audit --no-fund
docker build -t companion-sandbox:0.4 execution/sandbox
.\Check.ps1 -Docker
.\start_flight.ps1 -DryRun
```

`-DryRun` 仅预览命令，不校验配置。完成 [Flight](docs/Flight.md) 的地面检查后，使用唯一现场入口：

```powershell
.\start_flight.ps1 -AllowTakeoff
```

默认阿里；启动前用 `-VoiceProvider doubao` 选择备选，不在失败后自动切换。手动打开 http://127.0.0.1:18848，选择麦克风与耳机；两路只使用 SDK 无人机画面，不打开电脑摄像头。Ctrl+Shift+Alt+A / F12 停止，Ctrl+Shift+Alt+L / F11 降落。

## 接手阅读

先看 [Architecture](docs/Architecture.md) 的职责表、真实调用链及“改哪里”，再按工作查阅：

- [Setup](docs/Setup.md)：环境、账户凭据、入口参数。
- [API](docs/API.md)：模型可用的完整 Python / `drone` 契约和内部接口。
- [Testing](docs/Testing.md)：针对性验证、真实容器、显式云/硬件诊断及证据范围。
- [Flight](docs/Flight.md)：现场操作、耳机恢复、停止与退出。

本次简化把会话组装放在 `app`，一次程序的宿主能力集中在 `execution/Host.py`，跟随资源由 `FollowService` 持有，图像统一使用 SDK `FrameResult`。旧适配层、重复诊断和对应实现细节测试已退役；两家模型/endpoint 与手动选择音频设备的方式保留。

## 当前限制

- 新执行链与媒体恢复尚未通过真实云、蓝牙耳机、真机图传和现场网络的联合验收。回归通过不能证明飞行通过。
- 红帐篷不附标记；通用避障、精确测距、实际到达及旋转后主动找回用户均未验证。恢复跟随要求原标记重新可见。
- 导航把有界云语义种子关联到当前帧后再检查运动时效；这不能证明相机曝光年龄或解决端到端图传延迟。慢请求、跟踪丢失或歧义仍会拒绝运动。
- RC 不是物理速度，软件停止不是实机刹停保证；一次进程只控制一架飞机。
- 云端语音、视觉、任务与 RTC 可能分别计费。本地运行预算不代替账单或服务端费用上限。
