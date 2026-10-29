# 验证与诊断

测试验证当前独立行为，不按历史数量补回重复用例。软件、真实容器、云媒体、耳机和飞行分别验证；前一层通过不能替代后一层。现场流程见 [Flight](Flight.md)。

## 日常检查

```powershell
.\Check.ps1
.\Check.ps1 -Docker
```

默认运行离线 Python 与 JavaScript，强制跳过实际容器项。Docker 模式需要已构建 companion-sandbox:0.5；它验证真实容器，但仍不访问收费云或飞机。任一阶段失败，脚本非零退出并恢复原工作目录和环境。

| 修改范围 | 针对性检查 |
| --- | --- |
| 设备协议、授权、帧与遥测 | uv run --frozen pytest tests/sdk -q |
| 豆包配置、方舟视觉解析和跟踪 | uv run --frozen pytest tests/ai -q |
| 跟随控制、目标恢复和暂停交接 | uv run --frozen pytest tests/follow -q |
| Runtime、Host、取消、导航与容器 | uv run --frozen pytest tests/integration -q |
| RTC 服务、网页耳机、工具转发和画面 | npm --prefix web test |

pytest 只收集 tests；helpers 和 fixtures 不进入生产路径。直接运行 pytest 遵从 RUN_DOCKER_TESTS，保证默认离线行为应使用 Check。保留授权、取消竞争、真实容器复用／隔离／回收、跟随恢复、观察时效和媒体恢复等有独立意义的测试。

常驻容器检查覆盖：连续程序复用容器而不共享变量、程序残留进程与临时文件清理、取消后重建、退出回收。大量 print 只截断诊断内容；已捕获异常不覆盖程序最终成功状态。操作员急停不等待网页反馈。

## 显式诊断

先查看脚本 --help，再决定是否使用真实资源。diagnostics 不参与 pytest 自动收集。历史阿里诊断与 CloudReplay 实验数据用于追溯旧故障，不是当前豆包路线的验证入口。

```powershell
uv run --frozen python diagnostics/hardware/Ground.py --help
uv run --frozen python diagnostics/follow/check_follow_config.py --help
```

Ground／Video 保留 --self-check 与 --props-removed。FieldTakeoff、ContainerScenario 是需要注入 drone 的沙箱载荷，不直接独立运行。真实云请求须显式授权，凭据传文件路径；实际耳机和飞行实验由现场人员执行。

豆包云端验证需要区分三个范围：

- 官方 Ark SDK 的文字／图片请求，验证独立计划与结构化观察。
- StartVoiceChat／UpdateVoiceChat／StopVoiceChat，验证云任务接收、工具结果回填与停止确认。
- RTC 成员实际进房、发送音视频并接收工具，验证完整媒体与工具往返。单独 REST 请求成功不能替代这一步。

网页只使用选定的耳机和 SDK 无人机画面，不打开电脑摄像头。离线替身可以验证工具不等待最终字幕、去重、旧任务撤权与急停，但不能证明真实账号已启用 RTS 高级消息容量或蓝牙实际发声。工具完整程序的应用上限为 60000 字节，云传输仍受外层 JSON 大小与账号消息容量限制，见 [Setup](Setup.md)。

程序结果与原工具 ID 对应；云结果由 Python 经 Node 的 UpdateVoiceChat(function) 回填，网页只展示执行结果。确认模型后续真的调用工具或依据实际结果回答，不能把“我重新提交”这类口头承诺算作执行。

日志位于 logs/Runtime，Node 服务输出写入同一会话的 Rtc.log。检查工具到达、程序执行、结果回填、停止确认和清理记录；API 接收成功不等于媒体就绪、工具执行或实际动作完成。云端首音计时也不包含网页、声卡、蓝牙和飞机延迟。

### 突然下降的被动诊断

启动后自动记录到同一 Runtime JSONL，不另开 socket、不发额外查询命令、不改变控制逻辑。UDP/SDK 线程只写入现有有界内存队列；既有诊断循环每两秒增量落盘，设备关闭时再刷新，沿用 1 GB 日志保留策略。

- `flight_diagnostic.record`：每个收到的遥测包（包括 pitch/roll/yaw、vgx/vgy/vgz、agx/agy/agz、h/tof/baro、bat、time、温度），以及实际发送命令、发送间隔、来源、取消代次、原始回复、超时和丢弃回复。`recorded_at` 是事件发生时间，外层 `timestamp` 是落盘时间；使用 `sequence` 和 `monotonic` 还原顺序。回复时间是等待线程取出回复的时间，不是网卡收包时间，UDP 回复没有命令 ID。
- `flight_descent_observed`：三秒内遥测 h 降低至少 30 cm 时标记前后样本及最近 20 秒内存保留的命令。只标记现象，`cause=undetermined`，不会触发动作；完整前后遥测仍在相邻 flight_diagnostic 中。
- `stop_request_source`：区分模型中断、新程序替换、操作员热键、网页关闭／停止和应用退出。普通模型动作另与 program_start/journal 对照。
- `flight_diagnostic_gap` 和 device_diagnostics.flight_log_error：说明内存覆盖或记录失败。SessionLog.error/dropped_records 仍可能报告磁盘写入失败；不能把日志缺失解读成期间没有命令。进程崩溃可能丢失尚未落盘的末尾记录。

判读顺序：先找下降，再检查是否发过 down、负升降 RC、land/emergency；核对发送间隔、回复和遥测连续性；再比较姿态／加速度突变、电量变化、ToF/气压高度及电机运行时间。发送成功不证明飞机收到，低电量不自动证明低电量保护触发，低高度不自动证明已经落地。

官方依据：[SDK 2.0](https://dl-cdn.ryzerobotics.com/downloads/Tello/Tello%20SDK%202.0%20User%20Guide.pdf) 列出状态字段及 15 秒无命令自动降落；标准状态包没有电池电压、电机转速或自主降落原因字段。因此仅靠这些日志不能保证区分供电、碰撞或飞控自主保护，必要时需结合现场视频／指示灯或厂商诊断。

## 历史证据

ScenarioAcceptance/Evidence 保留旧版本数据，不作为当前实现通过的证明，也不保证原比较脚本仍存在。新诊断写入独立输出路径，保留失败、超时和清理结果；不覆盖历史输入或结论。日志不包含原图／音频，现场录像单独记录。

- [原跟随交接](history/FollowHandoff.txt)、[原安装说明](history/FollowSetup.txt)、[原控制说明](history/FollowControl.txt)：旧路径和操作不作为现行说明。
- [原项目快照](history/ProjectOverview.html)：旧链接可能失效，当前入口以 README 为准。
- [Companion 需求](../REQUIREMENTS.html/Companion(1).html)：原位保留。

当前链路仍需真实云工具往返、耳机恢复、图传延迟和真机逐轴联合验证。
