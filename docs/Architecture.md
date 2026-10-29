# 架构与维护入口

动作链：豆包 RTC 混合编排中的方舟模型生成结构化动作 → RTCBridge → UnifiedRuntime → ActionRunner → Host → 设备 SDK。没有模型 Python 执行器、容器或 Docker 依赖。

模型负责前摄像头观察和逐轮决策；宿主负责白名单动作执行、控制权交接、持续 RC 与急停。一个请求包含一个动作和对应位置参数，可选在该平移后调整跟随参考。完整契约见 [API](API.md)。

| 位置 | 职责 |
| --- | --- |
| app/Application.py、DeviceSession.py | plan/run/voice 入口、真实设备和退出组装 |
| app/Tools.py、Results.py | 工具 schema、参数校验、提示词及结果摘要 |
| app/Runtime.py、execution/ActionRunner.py | 动作准入、取消、截止时间、结果与清理 |
| execution/Host.py | SDK 授权、跟随交接、参考调整与动作分发 |
| providers/RTCBridge.py | 工具去重和调度、结果回填、图像推送 |
| providers/RTCRuntime.py、web/RTCService.js | Node 服务、RTC 临时授权、云任务生命周期 |
| web/RTCClient.js | 官方 RTC SDK、麦克风开关、耳机和无人机视频 |
| providers/DoubaoConfig.py、DoubaoVision.py | 凭据与官方 Ark 文字／视觉请求 |
| follow/r3_follow | 标记检测和固定跟随控制算法 |
| drone_sdk | 协议、遥测、最终发送检查和独立操作员急停 |
| app/SessionLog.py、FlightDiagnostics.py | 脱敏日志、1 GB 滚动保留和被动诊断 |
| app/ReplayCapture.py、web/ReplayRecorder.js、ReplayStore.js | 可选复盘录制，独立保存音频、视频、源码和时间线 |

## 语音循环

浏览器发布无人机画面及用户开启期间的麦克风音频。Node 调用 StartVoiceChat，配置 volcano_duplex 与 ArkV3 混合编排；原生语音交流，方舟模型处理视觉／工具动作。普通对话且不需画面或工具时使用服务配置的 NOVISION 规则。

RTCBridge 按工具 ID 去重，工具到达就执行，不等待最终 ASR 字幕。字幕仅用于展示与日志。新动作替换尚未结束的动作时先撤销旧授权并停止，迟到旧动作不能继续。普通对话不取消动作。

Runtime 每次创建独立 Host，ActionRunner 调用固定接口。Host 暂停已有跟随并申请 ControlGrant，SDK 发送前复核；动作结束释放控制权，成功时按票据恢复原跟随参考。结果由 Python 经 Node UpdateVoiceChat(function) 回填，网页仅展示。

运动收尾后请求更新帧，最多等待 2 秒确认浏览器发布；失败附带 camera_observation=unavailable，不改写动作结果或关闭会话。发布不证明云端采用该帧。模型看到真实新画面后再继续决策，直到用户目标完成、被取消或存在实际阻碍。

用户明确要求持续运动时，start_velocity 交给 Runtime 的独立 Host，每 100 ms 续发；正常动作结束或只读查询不停止。新运动、跟随切换、停止、失败及会话关闭会收尾。0.3 秒 RC 看门狗保留，结束后不自动恢复旧跟随。

## 跟随与运行边界

跟随固定 adaptive 横移／偏航、relative_size 前后、responsive_v1 参数。默认 RC 上限横向共享 50、前后 40、上下 20。高度 50–200 cm，距边界 20 cm 限制外向升降；未知或过期高度不授权升降。RC 和标记占比不代表真实速度或距离。

短时目标丢失或画面失效时归零等待新鲜稳定目标；显式停止、急停和关闭作废恢复意图。临时动作 pause_if_active / finish_action 保留原距离及构图，显式参考调整要求匹配轴平移和新帧。

ControlGrant 含 owner/control_epoch/command_epoch；同名 owner 不是同一次授权。取消同步撤销 generation，再异步停止。SDK 命令回复归属不明时保留 command_channel_ambiguous 和 command_uncertainty，但继续允许后续动作，不能据后来 ok 推断前一动作没发生。

清理未确认时保留待清理 Host，先重试，仅允许查询、停止及降落。accepted 不代表动作完成，发送后超时表示结果未知；保留底层 details，不自动重发。日志失败只记录缺失，不阻断设备清理。

媒体暂缺暂停对应媒体并保留控制连接，不更换设备或重放旧音频。操作员急停独立于云端，先请求停止再通知网页。运行日志分段约 16 MB，共享 1 GB 默认保留预算；复盘 Recordings 独立保留直到手动删除。

## 修改入口

工具契约改 app/Tools.py；动作交接改 Runtime、ActionRunner、Host；音频与网页改 web；云协议改 providers；跟随数值改 follow/configs；设备协议改 drone_sdk。运行 Check.ps1 检查软件，云、耳机、图传和真机按 [Flight](Flight.md) 单独验证。
