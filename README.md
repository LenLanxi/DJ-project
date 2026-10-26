# AI 随行小伙伴

目标：准备并起飞 → 跟随佩戴标记的用户 → 自然语言指令 → 朝红色帐篷移动并旋转一周 → 恢复跟随。
依据：`REQUIREMENTS.html/Companion(1).html`。当前是待现场验收的原型，不能将离线测试通过解释为真机任务通过。

## 正式结构

- `modules/rtc`：火山 RTC Web SDK，耳机音频、自定义无人机视频轨道、Seeduplex 混合编排与视觉模型。
- `modules/ai/main.py`：唯一正式入口、预检、会话期限、操作员热键与清理。
- `modules/ai/RTCBridge.py`：本机临时授权、RTC 工具调用、真实画面、断连处理。
- `modules/ai/sandbox_runner.py`、`sandbox/worker.py`：完整 Python 程序在 Docker 内执行；没有宿主机执行回退。
- `modules/ai/gateway.py`、`backends.py`：参数检查、控制授权编号、临时交接、结果与清理。
- `drone_sdk`：真实 Tello 驱动、遥测、最终发送检查、停止、降落及图传。
- `modules/follow`：第三组标记识别及可配置 PID。本轮保持算法不变；只迁移外层联调代码及更新 SDK 源码绑定。
- `tests`：替身、行为回归、真实容器检查和显式启用的云接口诊断。替身不进入正式执行路径。
- `ScenarioAcceptance/Evidence/Current`：本轮软件检查、云语音诊断和费用估算；`History` 与 `Results` 保留历史证据。均不能代替真机验收。

控制关系：RTC 模型 → 完整 Python → 隔离容器 → 网关 → SDK → Tello。跟随在电脑本地持续运行，云端只负责意图、观察和程序；两个模块共用同一连接。

## 环境

Windows；Python 3.11（uv 管理）；Node.js 22 以上；Docker Desktop Linux containers。电脑必须同时能连到 Tello Wi-Fi 与互联网，可使用独立网络接口或 USB 共享网络。

在仓库根目录运行：

```powershell
uv sync --frozen
npm --prefix modules/rtc ci --ignore-scripts --no-audit --no-fund
docker build -t companion-sandbox:0.4 modules/ai/sandbox
```

不要启动其他占用 Tello UDP 端口的程序。一次正式进程只连接一架飞机；三份跟随配置不构成多机协同。

## 云端配置

凭据放在当前进程环境，`.env.example` 仅为模板，不会自动加载：

- `RTC_APP_ID`、`RTC_APP_KEY`：AI 音视频互动应用，生成短期入房 Token。
- `VOLCENGINE_ACCESS_KEY_ID`、`VOLCENGINE_SECRET_ACCESS_KEY`：签名调用 VoiceChat OpenAPI。
- `VOLC_S2S_APP_ID`、`VOLC_S2S_TOKEN`：Seeduplex 服务凭据。
- `RTC_VLM_MODEL`、`RTC_VLM_API_KEY`：支持视觉与工具调用的方舟模型。本机 `observe` / `describe` 也使用它。

这些凭据用途不同。此前直连语音服务的 `X-Api-Key` 不能据此认定可替代 RTC AppKey、AK/SK 或视觉密钥。

正式云配置：OpenAPI `2025-06-01`；`S2SConfig.Provider="volcano_duplex"`、`OutputMode=1`；外挂 `LLMConfig` 开启 `VisionConfig.Enable=true`。长期凭据留在服务端，不发送给网页。

此版本的 `LLMConfig.ModelName` 支持模型 ID 或 `ep-` 开头的接入点 ID；自定义方舟资源还需 `APIKey`。不要改用旧版接口的 `EndPointId`。[官方配置](https://docs.volcengine.com/docs/real_time_communication/ConfigureLargeModelLLM?lang=zh)

官方依据：[RTC 混合编排](https://docs.volcengine.com/docs/real_time_communication/Accessend-to-endreal-timespeechlargemodel?lang=zh)、[视频理解](https://docs.volcengine.com/docs/real_time_communication/Videoandimageunderstanding?lang=zh)、[工具调用](https://docs.volcengine.com/docs/real_time_communication/FunctionCalling-1?lang=zh)、[官方示例](https://github.com/volcengine/rtc-aigc-demo)。

## 使用

先阅读 `FLIGHT_SOP.md`。只查看配置、不连设备：

```powershell
.\start_flight.ps1 -DryRun
```

完成现场检查后，用唯一启动器：

```powershell
.\start_flight.ps1 -AllowTakeoff
```

启动器输出本机页面地址；手动打开页面，明确选择麦克风和耳机，再连接。网页发布的是 SDK 图像绘制的画布视频，不开启电脑摄像头。浏览器设备 ID 与 sounddevice 数字索引不同，不能混用。

自然语言示例：“先检查状态，再起飞”“开始跟随”“前面有什么”“停止”“降落”。普通画面问答直接使用 RTC 视频；设备状态和动作须读取实际工具结果，再报告完成或失败。`accepted` 只代表接收请求；普通 `land()` 需要新鲜落地状态才能报告完成。

全局 Ctrl+Shift+Alt+A / F12 停止；Ctrl+Shift+Alt+L / F11 降落。操作员热键不依赖云端响应。默认退出请求降落；未确认实际落地会明确报告，退出进程不能证明飞机停止。

程序与文字诊断仍经同一容器、网关及 SDK，具体参数用 `uv run --frozen python modules/ai/main.py --help` 查询。没有正式模拟入口，也没有本地 STT/TTS 降级。

## 取消与会话

- 控制权与动作均绑定 `control_epoch` / `command_epoch`，不能仅靠同名 `model` 判断授权。
- 停止取消排队发送，并在实际写入 SDK 之前复查。已经发出的命令若被取消或超时，通道进入 command_channel_ambiguous：拒绝普通运动和非零 RC，只保留归零与优先降落；必须先核实落地，再明确断开重连。Tello 无命令编号，清空回复队列或等待几秒不能证明迟到回复已消失。物理刹停仍需真机测量。
- RTC 断连、心跳过期、图像过期或中断后撤销该会话授权。重新连接必须获得全新的 RTC 身份，旧 Bot 工具不能借新授权继续。
- RTC 工具消息没有可靠的用户轮次编号时，采用保守的重连策略，可能需要重说指令，也会失去云端历史。
- 主程序及云会话均有180秒整体时限，晚开页面不能重置。默认 RTC 工具通道1KB，程序限600个UTF-8字节，完整日志留在本机，回传摘要明确标注截断。关闭需同时停止 Bot 与退出真人 RTC 房间。停止失败会报告并保留重试信息；不能仅凭网页关闭声称费用已停止。

## 测试

```powershell
uv run --frozen pytest -q
$env:RUN_DOCKER_TESTS='1'
uv run --frozen pytest -q
Remove-Item Env:RUN_DOCKER_TESTS
npm --prefix modules/rtc test
```

常规回归不打开摄像头、麦克风或飞机连接，不调用收费云 API。Docker 项实际运行生成程序；SDK 协议测试运行真实 djitellopy 方法，只替换最终网络传输。第三组图像与反馈仿真仍不等于真实飞行。

`tests/LiveVoice.py --real` 是独立语音协议诊断，使用预录音及明确的假设备；它不是 RTC 正式入口。没有 `--real` 不会收费；`--real-sandbox` 可让状态查询真正经过容器。完整 RTC 混合编排需另做真实入房、音视频及视觉验证，不能沿用直连语音的通过结论。

## 当前必须知道的限制

1. 尚未完成真机、蓝牙耳机、RTC 云端混合编排及现场网络的联合验收。
2. 红色帐篷没有附加标记；当前没有经过验证的通用目标导航、避障或精确距离测量。图像占比不能证明实际到达。
3. `observe` 要求解码后年龄不超过0.5秒，云推理过慢会拒绝移动；这个时间戳不是相机曝光时间。本地 H.264/UDP 实验已复现实际画面旧约1.67秒、解码年龄仍为零。已修复超大 FIFO／溢出策略，但端到端图传延迟尚未解决；RTC 问答也不能自动证明导航证据时效。
4. 旋转一周后，原用户可能已不在视野。现有跟随依赖原标记重新可见，不承诺主动搜索或身份重识别。
5. 第三组默认配置的 RC 上限是接口请求值，尚未实机标定，不是米/秒或角速度保证。
6. 云端任务、RTC 时长、语音和视觉可能分别计费。短会话与用量记录用于限制测试开销，不能代替账户账单或服务端费用上限。

旧 Agent / Demo / Session / ProgramRunner、固定口令、分段 STT/TTS、本地语音降级、重复 docs 实现及旧启动器已经移除。有价值的保护转入当前测试；旧版本可从 Git 历史追溯，不作为备用入口。
