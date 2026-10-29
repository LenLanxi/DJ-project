# AI 随行小伙伴

面向 Tello 的原型：跟随佩戴标记的用户，通过耳机持续语音交互，结合无人机画面生成结构化动作、执行任务并反馈真实结果。保留自然目标导航、临时任务后恢复原跟随参考。需求来源为 [Companion 原始页面](REQUIREMENTS.html/Companion(1).html)。

## 安装与启动

需要 Windows、uv 管理的 Python 3.11；Node.js 22+ 用于本机 RTC 服务和网页测试。电脑须同时连接 Tello Wi-Fi 与互联网。豆包 RTC 是唯一模型路线，凭据配置见 [Setup](docs/Setup.md)。在仓库根目录执行：

```powershell
uv sync --frozen
npm --prefix web ci --ignore-scripts --no-audit --no-fund
.\Check.ps1
.\start_flight.ps1 -DryRun
```

`-DryRun` 只预览命令。完成 [Flight](docs/Flight.md) 的地面检查后运行：

```powershell
.\start_flight.ps1 -AllowTakeoff
```

手动打开 http://127.0.0.1:18848，选择麦克风与耳机。连接后点击“开始说话”，说完点击“发送并静音”；麦克风默认不采集。页面只接收真实 SDK 无人机画面，不打开电脑摄像头。Ctrl+Shift+Alt+A / F12 停止；Ctrl+Shift+Alt+L / F11 降落。

## 接手阅读

- [Architecture](docs/Architecture.md)：职责、调用链、控制交接。
- [Setup](docs/Setup.md)：环境、豆包凭据和入口参数。
- [API](docs/API.md)：结构化动作接口。
- [Testing](docs/Testing.md)：针对性检查、显式云／硬件诊断。
- [Flight](docs/Flight.md)：现场操作、耳机恢复和退出。

应用有 plan、run、voice 三个入口；用 `uv run --frozen python -m app.Application <入口> --help` 查看参数。现场启动器默认 voice，`-ActionFile` / `-Text` 选择 run，`-Text ... -PlanOnly` 选择 plan。

## 当前限制

- 无通用避障、精确测距或实际到达保证；旋转后恢复跟随需要原标记重新可见。
- RC 是请求单位，不是物理速度；标记大小不是距离传感器。一次进程控制一架飞机。
- 语音任务按“看前摄像头→提交一个结构化动作→执行→看新画面”循环；宿主只接受白名单动作与对应参数，不执行模型代码。图传解码时间不是相机曝光时间，浏览器提交新帧也不证明云端已收到。
- 豆包实时语音由火山 RTC 混合编排接入：原生语音负责交流，方舟模型负责视觉和结构化工具动作。
- 云端调用可能计费，本地不设金额限制。
