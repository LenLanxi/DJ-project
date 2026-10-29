# AI 随行小伙伴

面向 Tello 的原型：跟随佩戴标记的用户，通过耳机持续语音交互，结合无人机画面生成完整 Python 程序、执行任务并反馈真实结果。保留自然目标导航、临时任务后恢复原跟随参考；尚未完成整段真机联合验收。需求来源为 [Companion 原始页面](REQUIREMENTS.html/Companion(1).html)。

## 安装与启动

需要 Windows、uv 管理的 Python 3.11、Docker Desktop Linux containers；Node.js 22+ 用于本机 RTC 服务和网页测试。电脑须同时连接 Tello Wi-Fi 与互联网。豆包 RTC 是唯一模型路线，凭据配置见 [Setup](docs/Setup.md)。在仓库根目录执行：

```powershell
uv sync --frozen
npm --prefix web ci --ignore-scripts --no-audit --no-fund
docker build -t companion-sandbox:0.5 execution/sandbox
.\Check.ps1 -Docker
.\start_flight.ps1 -DryRun
```

`-DryRun` 只预览命令。完成 [Flight](docs/Flight.md) 的地面检查后运行：

```powershell
.\start_flight.ps1 -AllowTakeoff
```

手动打开 http://127.0.0.1:18848，选择麦克风与耳机。页面只接收真实 SDK 无人机画面，不打开电脑摄像头。Ctrl+Shift+Alt+A / F12 停止；Ctrl+Shift+Alt+L / F11 降落。

## 接手阅读

- [Architecture](docs/Architecture.md)：职责、调用链、控制交接和容器复用。
- [Setup](docs/Setup.md)：环境、豆包凭据和入口参数。
- [API](docs/API.md)：完整 Python 与 drone 接口。
- [Testing](docs/Testing.md)：针对性检查、真实容器和显式云／硬件诊断。
- [Flight](docs/Flight.md)：现场操作、耳机恢复和退出。

应用有 plan、run、voice 三个入口；用 `uv run --frozen python -m app.Application <入口> --help` 查看参数。现场启动器默认 voice，`-Program` / `-Text` 选择 run，`-Text ... -PlanOnly` 选择 plan。

## 当前限制

- 软件回归不证明真实云、蓝牙耳机、图传和飞行联合通过。
- 无通用避障、精确测距或实际到达保证；旋转后恢复跟随需要原标记重新可见。
- RC 是请求单位，不是物理速度；标记大小不是距离传感器。一次进程控制一架飞机。
- 图传解码时间不是相机曝光时间；自然目标导航仍依赖有效跟踪和新鲜观察。
- 豆包实时语音由火山 RTC 混合编排接入：原生语音负责交流，方舟模型负责视觉和工具程序。
- RTC 工具消息默认容量为 1 KB，需要在控制台启用 RTS 高级功能才能使用最多 64 KB 的消息。程序的 60000 字节应用上限不保证转义后的完整消息能容纳，详见 [Setup](docs/Setup.md)。
- 云端调用可能计费，本地不设金额限制。
