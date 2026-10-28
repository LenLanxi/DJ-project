# 验证、诊断与历史证据

测试按行为验证当前实现，不以文件数量或历史通过数字作为完成标准。软件、实际容器、真实云媒体、耳机和飞行是不同验证层；前一层通过不能替代后一层。现场步骤见 [Flight](Flight.md)。

## 日常与针对性检查

```powershell
.\Check.ps1
.\Check.ps1 -Docker
```

默认依次运行离线 Python 与 JavaScript，强制跳过实际容器项，即使调用者已有 `RUN_DOCKER_TESTS=1`。`-Docker` 显式开启实际容器，需要已构建 `companion-sandbox:0.4`。任一阶段失败就非零退出；脚本恢复原工作目录与环境变量。两种模式均不打开飞机、相机、麦克风或收费云 API。

修改时先运行对应层，再做完整 Check；程序/RPC/容器生命周期改动另加 `-Docker`：

| 修改范围 | 针对性命令 |
| --- | --- |
| 设备协议、授权、帧与遥测 | `uv run --frozen python -B -m pytest tests/sdk -q` |
| 模型协议、视觉解析和跟踪 | `uv run --frozen python -B -m pytest tests/ai -q` |
| 跟随算法、时效、暂停/恢复 | `uv run --frozen python -B -m pytest tests/follow -q` |
| Runtime、Host、取消、导航与跨模块 | `uv run --frozen python -B -m pytest tests/integration -q` |
| RTC 会话与工具契约 | `uv run --frozen python -B -m pytest tests/rtc -q` |
| 网页设备、音频、视频和服务端结果 | `npm --prefix web test` |

pytest 只收集 `tests`；`tests/helpers` 提供显式设备/协议替身，`tests/fixtures` 保存固定输入，均不进入生产路径。直接运行 pytest 遵从 `RUN_DOCKER_TESTS`；要保证默认离线行为用 Check。实际容器测试带 docker 标记，但标记本身不授权执行。

关键断言包括：旧任务不借新授权、取消前后最终发送检查、容器创建/删除确认、清理未确认只保留恢复能力、跟随 stop 与 close 不混淆，以及帧身份/时效不因重复读取而刷新。Runtime 测试显式注入 device/follow/vision/runner；脚本 runner 替身不声称实际执行了 Docker。重复适配层、示范和实现字段专用测试已经退役，其仍有价值的行为断言由当前边界测试覆盖。

## 显式诊断

`diagnostics` 不参加 pytest 自动收集；先看脚本帮助再选资源与参数。目录名不保证所有脚本都访问该种资源。

| 目录 | 当前用途 |
| --- | --- |
| `diagnostics/cloud` | 两家文件媒体与工具对照、Omni 会话/重叠输入、归因；CompareRealtime/RouteAnalysis 为离线分析 |
| `diagnostics/hardware` | Ground/Video 地面与图传检查、FieldProtocol、FieldTakeoff；VideoProbe 是本机 loopback 解码实验，不打开真实硬件 |
| `diagnostics/local` | Environment 环境存在性检查、IntegrationSmoke、ContainerScenario 载荷、OmniPlayback 播放队列复核 |
| `diagnostics/follow` | 配置检查、相机/物理标定、生成/打印标记；生产跟随在 `follow` |

```powershell
uv run --frozen python diagnostics/cloud/RunRealtime.py --help
uv run --frozen python diagnostics/hardware/Ground.py --help
uv run --frozen python diagnostics/follow/check_follow_config.py --help
```

Ground/Video 保留 `--self-check` 与 `--props-removed`。FieldTakeoff、ContainerScenario 是需要注入 drone 的沙箱载荷，不直接当独立飞行脚本运行。云请求必须显式 `--real`；先核对凭据文件、输入、次数与账户状态，不把密钥写命令行、网页或证据。正式对照串行、同输入，保留失败与超时，调试样本不混进排名。相机/硬件实验由现场人员显式执行，不能由回归代为证明安全或成功。

`providers/AlibabaRealtime.py` 保留生产共享协议；单轮基准在 `diagnostics/cloud/AlibabaProbe.py`，计时计算在 RealtimeMetrics。豆包原生 `web/NativeProbe.cpp` 不采集设备，与网页编解码/缓冲不同。腾讯与旧直连语音实现已退役，历史三路线数据不代表当前可重跑腾讯。

## 采集与证据解释

新云采集默认位于 `ScenarioAcceptance/Evidence/Current/RealtimeComparison` 或 `Current/RouteSelection`，可用 `--output-folder` 改位置。历史输入、失败、HTML 与结论不覆盖；重算写到归档以外：

```powershell
uv run --frozen python diagnostics/cloud/CompareRealtime.py --folder ScenarioAcceptance/Evidence/RealtimeComparison --output ScenarioAcceptance/Evidence/Current/RealtimeSummary.json
uv run --frozen python diagnostics/cloud/RouteAnalysis.py --folder ScenarioAcceptance/Evidence/RouteSelection20261027 --output ScenarioAcceptance/Evidence/Current/RouteAnalysis.json
```

这两条只离线分析。`node diagnostics/local/OmniPlayback.js <OmniLive记录.json>` 用模拟时钟重放分块节奏，不证明耳机实际发声。

`RealtimeComparison` 是首轮文件媒体比较；`RouteSelection20261027`、`ProviderAttribution20261027` 是后续选型/归因；`R3Integration` 为跟随集成；`Current`、`History`、`Results` 保留各自日期/版本的其他证据。部分现场媒体只存在测试机，不随源码提交。历史文件名、行号、测试数量或语义成功率不能代表本次重构的现状。

比较须区分完整输入、语义回答、实际工具结果、媒体送达与清理。首音计时不含网页、声卡、蓝牙或飞行延迟；小样本仅描述当时网络。供应商接受配置不证明参数实际生效；视觉答对也不证明导航可执行或到达。原生 SDK、Token 和原始 PCM 不入库，来源/复现要求见对应 Setup.json；本机 `web/Build` 不是可移植的完整环境。

当前新链路仍需真实云工具往返、耳机暂缺/恢复、图传新鲜度及真机逐轴检查。历史局部通过或故障单次复测不能直接证明新实现通过联合验收，也不能证明历史时序故障根因消失。

## 历史入口

- [FollowHandoff.txt](history/FollowHandoff.txt)、[FollowSetup.txt](history/FollowSetup.txt)、[FollowControl.txt](history/FollowControl.txt)：原跟随交接原文，旧路径与操作不作为现行说明。
- [ProjectOverview.html](history/ProjectOverview.html)：2026-10-27 原文快照；相对链接仍按旧根目录，迁移后部分不可用，代码导航以当前 README 为准。
- [Companion 原始需求](../REQUIREMENTS.html/Companion(1).html)：保留原位。

许可证、来源记录和既有历史证据原样保留；当前可执行入口以本页和源码为准。
