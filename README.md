# AI 随行小伙伴

目标：准备并起飞 → 跟随佩戴标记的用户 → 自然语言指令 → 朝红色帐篷移动并旋转一周 → 恢复跟随。
依据：`REQUIREMENTS.html/Companion(1).html`。当前是待现场验收的原型，不能将离线测试通过解释为真机任务通过。

历史导览：[执行过程与代码全局情况（HTML快照）](ProjectOverview.html)。旧报告保持原样；当前入口、结构及限制以本README和源码为准。

## 正式结构

- `modules/rtc`：默认阿里网页音频客户端（`Omni.html`、`OmniClient.js`、`OmniAudio.js`）；保留豆包 RTC Web SDK 客户端作为显式备选。两路只接收 SDK 无人机画面，不打开电脑摄像头。
- `modules/ai/main.py`：唯一正式入口、预检、会话期限、操作员热键与清理。
- `modules/ai/RTCBridge.py`：本机临时授权、RTC 工具调用、真实画面、断连处理。
- `modules/ai/VoiceRoutes.py`：服务选择及服务端凭据预检；`OmniRuntime.py`、`OmniSession.py`：阿里本机传输、真实多轮云会话、输入/回复/工具编号与本地授权隔离。
- `modules/ai/sandbox_runner.py`、`sandbox/worker.py`：完整 Python 程序在 Docker 内执行；没有宿主机执行回退。
- `modules/ai/gateway.py`、`backends.py`：参数检查、控制授权编号、临时交接、结果与清理。
- `drone_sdk`：真实 Tello 驱动、遥测、最终发送检查、停止、降落及图传。
- `modules/follow`：合并三组 `DJ-project-R3@b2ce3d2` 的 ArUco、可配置 PID、自适应横移/转向、相对距离、提前减速与上下跟随；来源见 `modules/follow/R3Source.json`。
- `tests`：替身、行为回归、真实容器检查和显式启用的云接口诊断。替身不进入正式执行路径。
- `ScenarioAcceptance/Evidence/R3Integration`：本次三组集成回归；`Current` 保留前次 RTC 重构、云语音诊断与费用记录；`History` 与 `Results` 为更早证据。均不能代替真机验收。

控制关系：所选语音模型 → 完整 Python → 隔离容器 → 网关 → SDK → Tello。跟随在电脑本地持续运行，云端只负责意图、观察和程序；两个模块共用同一连接。两路共用执行、取消及跟随交接链路。

正式启动器的网页入口固定为 http://127.0.0.1:18848，两条语音路线一致。可用 `-WebPort 18849` 显式更改；占用时报错，不自动选择其他端口。会话重启后刷新原网页，重新取得本轮授权。

阿里网页播放器将云端快速返回的语音放入有上限的队列：近6秒安排播放，已安排与待播合计最多30秒。超过总量上限、播放时钟停滞2秒或设备断开时停止；正常快速生成超过6秒的回复不会直接被当作耳机故障。停止与新输入立即清空全部待播音频；网页退出原因及队列状态写入运行日志。`tests/OmniPlayback.js <OmniLive记录.json>` 可按真实云端分块节奏离线复核生产播放队列，使用模拟时钟，不代表真实蓝牙播放验收。

## 当前默认与备选

默认 `alibaba`（Qwen3.8 Omni Flash Realtime），显式备选 `doubao`（Seeduplex + RTC + Ark 视觉）；腾讯不参加当前选型。阿里网页将耳机 PCM 经本机服务器上传，SDK JPEG 每秒送云；豆包网页使用 RTC 音视频轨道。长期密钥都留在服务端。

独立对照把输入提交、轮次、模型语义和工具执行分开记录：同一完整文本与同一 JPEG 的8组场景，两家核心语义均8/8通过；阿里有1次额外只读工具调用。不能据此称阿里模型更聪明。选阿里作为默认，依据是此前相同录音的较低首音延迟、原生工具回传和可直接控制的输入协议；原 RTC 1KB 消息限额也使豆包正式程序仅能传600字节，阿里允许完整程序最多60000个UTF-8字节。

额外媒体控制保留了反例：同一压缩停顿录音，豆包在完整同轮输入后正确回答；阿里在`semantic_vad/800ms`下未出现语音检测/提交事件。后者不是正式`server_vad/1200ms`配置，属于输入未确认，不能算模型答错，也不支持普遍可靠性排名。原始输入、协议确认、额外工具和费用估算留存在测试机器的 `ScenarioAcceptance/Evidence/ProviderAttribution20261027`（本次源代码提交不包含这些现场产物）；`tests/ProviderAttribution.py`可离线重判，云调用必须显式`--real`。历史首音数据不能代表网页/蓝牙耳机的实际播放延迟。

备选只在启动前手动选择；真实断连或失败后先停止并核对飞机状态，重新启动入口，不自动换服务、重放旧指令或恢复旧授权。阿里保持VAD检测与转录，关闭自动回复和声音起点自动打断：声音、空转录或转录失败不取消动作；完整转录后请求模型回复，模型提出新的工具程序时才停止旧程序、确认清理并执行新程序，语音会话保持连接。普通对话不取消旧程序。每次只允许一个尚未结束的云端回复请求，旧回复的done确认及SDK授权编号共同阻止迟到结果借新授权执行；停止或清理未确认时拒绝新程序。全局停止/降落热键仍独立于云端，语音接管需等待转录和模型判断，不能代替紧急热键。

## 2026-10-28 更新与验证边界

- 默认本机网页端口改为 18848。豆包单张 RTC 令牌最多有效一小时，SDK 到期回调续签；续签不延长原会话期限。
- 模型可在同一用户任务中多次调用程序。豆包参数错误或代码超过 600 字节时由本机拒绝并将错误返回模型，保留语音连接。
- 日志新增网页停止来源、错误提示和工具参数摘要。历史橙色椅子任务在程序执行前中断，旧日志不足以还原两次网页错误，尚不能声称导航通过。
- 国内独立视觉决策模型与短程序逐步反馈的新架构仍处于方案阶段，未并入生产入口。
- 跟随测试和离线演示已同步一秒时效：保留 0.6 秒有效、超过一秒归零、旧帧不能刷新时效及检测预算保护。视频解码已限制打开、探测及读取的单阶段等待，停止后不再开始新读取；若退出仍超时，会报告失败并保留清理重试入口。真实本机 UDP 过载、无数据退出与端口释放回归已通过；语音提前工具调用和降落后清理的历史时序失败单独复测通过，尚不能认定根因已修复。局部测试通过不能代表完整实机验收。凭据、运行日志及本次现场媒体不随源码上传。

## 三条实时路线的候选测试

以下保留历史三路线候选测试的范围，不代表当前正式入口或当前模型优劣：

- `modules/ai/AlibabaRealtime.py`：Qwen3.8 Omni WebSocket，真实 PCM、JPEG、原生 Function Calling 与工具结果回传；有会话期限、取消、输入预算和免费额度声明检查。它不在宿主机执行模型代码。
- `modules/ai/TencentConversation.py`：TRTC 云签名、短期 UserSig、Start/Describe/Control/Stop、失败清理。当前公开接口是 STT→外部 LLM→TTS；不能仅配置视觉模型名称便宣称视频/客户端工具已接通。
- `modules/rtc/NativeProbe.cpp`、`TencentProbe.cpp`：两家官方原生 SDK 的无窗口媒体测量客户端，不采集设备；与正式网页客户端的编解码、缓冲不同。
- `tests/AlibabaBenchmark.py`、`DoubaoBenchmark.py`、`TencentBenchmark.py`：只有显式 `--real` 才调用云；统一文件输入、工具使用假设备，所有尝试包括失败均保留。真实调用不会被常规 pytest 或 CI 自动运行。

协议依据：[阿里实时多模态与工具](https://help.aliyun.com/zh/model-studio/realtime)、[腾讯对话公开接口](https://cloud.tencent.com/document/api/647/108514)、[腾讯工具代理示例](https://github.com/Tencent-RTC/trtc-conversation-ai-example/tree/main/llm_function_call)。两家 RTC 的准确 SDK 来源及本地构建方式见 `ScenarioAcceptance/Evidence/RealtimeComparison/*Setup.json`；SDK 二进制、临时入房凭据和原始输出 PCM 留在忽略的 `modules/rtc/Build`，不提交。

首轮历史测量（不代表后续修复配置）：首音中位数阿里1.41秒、腾讯2.61秒、豆包3.62秒。三路媒体均送达3/3；严格字幕复述分别3/3、1/3、0/3。豆包当时通过红蓝图与容器工具往返；阿里自动图像/工具失败、手动提交隔离成功。实际paragraph是布尔结束标记、roundId才是轮次；网页已按真实协议修复并补回归。

后续选型对照独立保存在 `ScenarioAcceptance/Evidence/RouteSelection20261027`，原 HTML 与首轮证据保持原样。`tests/RouteSelection.py` 按阶段显式 `--real` 运行；`tests/RouteAnalysis.py` 仅离线分析。新判据要求完整指令处于同一轮、真实非静音回复、实际工具结果和确认清理；颜色答对但问题被截断也不算整轮通过。阿里新增可配置声学断句、每秒图片流、完整转录与生效配置记录，以及工具期间新语音的取消保护。豆包防抢话参数仅在独立 Probe 中显式试验，未改变正式飞行入口或跟随算法。每份后续 HTML 报告均须另建文件，不覆盖历史报告。

后续46次媒体对照支持优先集成阿里：新语音中位数阿里1.87秒、豆包3.10秒、腾讯3.28秒；阿里完整红蓝问题6/6通过，工具重复2/3通过（另一次超时保留），取消保护后工具2/2通过。豆包默认配置也出现拆轮，官方防抢话参数试验未证明可稳定避免。工具计时包含实际本地容器耗时，Confirm探索阶段与本地回归有重叠，不用于纯服务速度排名。这是历史候选阶段的范围；当前默认已改为阿里，但真实耳机、图传、最终设备取消与整段飞行仍需现场验收。

测试结果在 `ScenarioAcceptance/Evidence/RealtimeComparison`。`Summary.json` 分别记录语音、视觉对照与工具往返；Start 接口耗时不参加语音排名。计时从统一录音最后有声采样发出开始，首音频指收到/解码出的语音，未包含声卡、网页播放、蓝牙耳机或真实飞行延迟。三次重复只描述本机网络条件，不是普遍性能保证。

离线重算结果，不调用云或设备：

```powershell
uv run --frozen python tests/CompareRealtime.py
```

重新收费/消耗试用额度前，先查看各 `*Benchmark.py --help`，显式指定服务端凭据文件；密钥不写命令行或测试证据，不传进网页。正式比较须串行运行、使用相同输入，开发排错样本不能混入正式排名。

启动器默认 `aircraft_A_ID0_motion_follow.json`：ID0、adaptive、responsive_v1、relative_size、上下跟随；横向共享预算50 RC、前后40 RC、上下20 RC。高度配置50–200cm，距边界20cm时拦截继续外向升降；高度遥测未知/过期时升降归零。RC不是物理速度，高度保护不是实际刹停保证。参考标记平均边长占图宽的比例上限0.20，默认不是米制测距；pose模式需要另行测量标记及标定相机。

临时程序默认保留原跟随距离/构图。用户明确调整跟随位置时，可在对应轴移动后调用 `adjust_follow_reference('depth'/'horizontal'/'vertical')`；等待动作后新帧且整段程序成功才更新并恢复。该调用返回accepted时尚未应用，最终工具结果的follow_cleanup提供确认。红帐篷等临时任务不应重设原跟随基准。短暂丢失默认归零等待最多30秒，旧帧/异常仍停止；旋转后恢复仍要求原标记可见。

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

启动器默认读取服务端文件 `Desktop/tmp/DomesticAiTrials/AlibabaCredentials.json` 与 `Desktop/tmp/DoubaoRealtime/Credentials.json`，可用 `-AlibabaCredentials`、`-DoubaoCredentials` 或 `ALIBABA_CREDENTIALS_FILE`、`DOUBAO_CREDENTIALS_FILE` 指定。阿里文件提供 ApiKey、WorkspaceId、WebSocketUrl、Model、Region；两路导航所用方舟凭据均来自豆包文件。所有必要配置在连接飞机前检查。

主入口默认不授权付费；启动器基于本次授权默认传 `-ApiBudgetRmb 15`，它是付费授权金额声明；本地只限制时长、轮数及输出token，并未按人民币累计停机，也不是服务端硬限费或账户余额证明。凭据声明免费不能证明方舟等其他环节免费。正式语音目前要求显式服务端JSON文件；以下是文件内容对应的底层RTC环境字段名称，不构成无文件启动方式。`.env.example`仅为模板，不会自动加载：

- `RTC_APP_ID`、`RTC_APP_KEY`：AI 音视频互动应用，生成短期入房 Token。
- `VOLCENGINE_ACCESS_KEY_ID`、`VOLCENGINE_SECRET_ACCESS_KEY`：签名调用 VoiceChat OpenAPI。
- `VOLC_S2S_APP_ID`、`VOLC_S2S_TOKEN`：Seeduplex 服务凭据。
- `RTC_VLM_MODEL`、`RTC_VLM_API_KEY`：支持视觉与工具调用的方舟模型。本机 `observe` / `describe` 也使用它。

这些凭据用途不同。此前直连语音服务的 `X-Api-Key` 不能据此认定可替代 RTC AppKey、AK/SK 或视觉密钥。

豆包备选的云配置：OpenAPI `2025-06-01`；`S2SConfig.Provider="volcano_duplex"`、`OutputMode=1`；外挂 `LLMConfig` 开启 `VisionConfig.Enable=true`。长期凭据留在服务端，不发送给网页。

RTC 的 `LLMConfig.ThinkingType="disabled"` 显式关闭深度思考，避免沿用模型默认思考模式而增加实时视觉结果过期的概率。它不能保证图像到动作的延迟达标，仍需实测。[官方实时 LLM 配置建议](https://docs.volcengine.com/docs/real_time_communication/ConfigureLargeModelLLM?lang=zh)

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

启动器输出本机页面地址；手动打开页面，明确选择麦克风和耳机，再连接。默认阿里上传耳机音频及服务端 SDK JPEG；豆包备选发布 SDK 图像绘制的画布视频。两路均不开电脑摄像头。浏览器设备 ID 与 sounddevice 数字索引不同，不能混用。

启动前改用备选：`.\start_flight.ps1 -VoiceProvider doubao -AllowTakeoff`。阿里失败不会自动切换或重放；应先停止、核对飞机状态，再重启入口选择备选。

自然语言示例：“先检查状态，再起飞”“开始跟随”“前面有什么”“停止”“降落”。普通画面问答直接使用所选路线的当前图像；设备状态和动作须读取实际工具结果，再报告完成或失败。`accepted` 只代表接收请求；普通 `land()` 需要新鲜落地状态才能报告完成。

全局 Ctrl+Shift+Alt+A / F12 停止；Ctrl+Shift+Alt+L / F11 降落。操作员热键不依赖云端响应。默认退出请求降落；未确认实际落地会明确报告，退出进程不能证明飞机停止。

程序与文字诊断仍经同一容器、网关及 SDK，具体参数用 `uv run --frozen python modules/ai/main.py --help` 查询。没有正式模拟入口，也没有本地 STT/TTS 降级。

## 取消与会话

- 控制权与动作均绑定 `control_epoch` / `command_epoch`，不能仅靠同名 `model` 判断授权。
- Docker 取消先在启动客户端仍存活时确认容器移除，再结束客户端；创建未确认时，单次“找不到容器”不算清理成功。超过期限或清理被再次取消会保留容器名称、错误及 `runner.last_cleanup`，明确报告未确认；跟随收尾异常也不能覆盖这些证据。
- 停止取消排队发送，并在实际写入 SDK 之前复查。已经发出的命令若被取消或超时，通道进入 command_channel_ambiguous：拒绝普通运动和非零 RC，只保留归零与优先降落；必须先核实落地，再明确断开重连。Tello 无命令编号，清空回复队列或等待几秒不能证明迟到回复已消失。物理刹停仍需真机测量。
- RTC 断连、心跳过期、图像过期或中断后撤销该会话授权。重新连接必须获得全新的 RTC 身份，旧 Bot 工具不能借新授权继续。
- RTC 工具消息没有可靠的用户轮次编号时，采用保守的重连策略，可能需要重说指令，也会失去云端历史。
- 主程序及云会话默认30000秒整体时限，晚开页面不能重置。阿里每轮一次程序，程序最多60000个UTF-8字节；只保留最近32个输入记录，声音检测不消耗执行轮次。回复编号与工具调用记录另有内存保护上限。豆包备选工具通道1KB，程序限600字节。完整日志留在本机，回传摘要明确标注截断。关闭豆包需同时停止 Bot 与退出真人 RTC 房间；阿里关闭云 WebSocket。停止失败会报告并保留重试信息；不能仅凭网页关闭声称费用已停止。

## 测试

```powershell
uv run --frozen pytest -q
$env:RUN_DOCKER_TESTS='1'
uv run --frozen pytest -q
Remove-Item Env:RUN_DOCKER_TESTS
npm --prefix modules/rtc test
```

常规回归不打开摄像头、麦克风或飞机连接，不调用收费云 API。Docker 项实际运行生成程序；SDK 协议测试运行真实 djitellopy 方法，只替换最终网络传输。第三组图像与反馈仿真仍不等于真实飞行。

`tests/LiveVoice.py --real` 是独立语音协议诊断，使用预录音及明确的假设备；它不是 RTC 正式入口。没有 `--real` 不会收费；`--real-sandbox` 可让状态查询真正经过容器。本轮原生 SDK 已有真实入房、合成媒体与视觉／工具记录；正式网页与实物耳机、无人机图传的联合验收仍需另做，不能沿用直连语音的通过结论。

## 当前必须知道的限制

1. 默认阿里和备选豆包均有真实云文件媒体证据；尚未完成正式网页客户端、蓝牙耳机、真机图传及现场网络的联合验收。
2. 红色帐篷没有附加标记；当前没有经过验证的通用目标导航、避障或精确距离测量。图像占比不能证明实际到达。
3. `observe` 要求解码后年龄不超过0.5秒。真实 Seed 2.1 Lite 在同一合成图上，默认思考约16.7秒；关闭思考后约2.1秒，均被拒绝移动。这是当前红帐篷导航的实际阻碍，不能靠接口开通或离线测试通过掩盖。这个时间戳也不是相机曝光时间：本地 H.264/UDP 实验已复现实际画面旧约1.67秒、解码年龄仍为零。已修复超大 FIFO／溢出策略，但端到端图传延迟尚未解决；RTC 问答不能自动证明导航证据时效。
4. 旋转一周后，原用户可能已不在视野。现有跟随依赖原标记重新可见，不承诺主动搜索或身份重识别。
5. 第三组默认配置的 RC 上限是接口请求值，尚未实机标定，不是米/秒或角速度保证。
6. 云端任务、RTC 时长、语音和视觉可能分别计费。短会话与用量记录用于限制测试开销，不能代替账户账单或服务端费用上限。

旧 Agent / Demo / Session / ProgramRunner、固定口令、分段 STT/TTS、本地语音降级、重复 docs 实现及旧启动器已经移除。有价值的保护转入当前测试；旧版本可从 Git 历史追溯，不作为备用入口。
