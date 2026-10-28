# 架构与维护入口

应用只有一条动作链：模型生成完整 Python，在 Docker 内运行，经宿主能力检查后调用同一个 SDK。跟随在本机持续识别标记，临时程序通过控制票据暂停、接管与恢复。模型接口见 [API](API.md)，验证入口见 [Testing](Testing.md)。

## 职责与所有权

| 位置 | 负责什么 |
| --- | --- |
| `app/Application.py` | Python 入口、参数单点校验和每次会话的组装/关闭顺序 |
| `app/DeviceSession.py` | 创建并关闭设备、图传与跟随服务；不实现算法 |
| `app/Runtime.py` | `UnifiedRuntime`：一次程序准入、取消、容器任务及宿主清理 |
| `app/Tools.py` | 两家共用的模型提示词、工具定义、参数和程序预算 |
| `app/Results.py` / `SessionLog.py` | 对外结果与脱敏运行日志 |
| `providers` | 凭据、路线选择、供应商事件、会话/工具编号和云结果回传 |
| `web` | 阿里 PCM/JPEG 页面、豆包 RTC 页面、手动音频设备与媒体状态 |
| `execution/Host.py` | 单段程序的能力范围、授权、跟随交接、导航证据及 RPC 分发 |
| `execution/sandbox_runner.py` / `sandbox/worker.py` | Docker 启停、受限 RPC、生成 Python 的容器内执行，无宿主回退 |
| `navigation` | JPEG 问题契约、唯一结构化解析、图像稳定及目标跟踪 |
| `follow/r3_follow/service.py` | `FollowService`：常驻识别、跟随控制和暂停/恢复票据 |
| `follow/r3_follow/core.py` / `vision.py` / `configs` | 跟随控制律、ArUco 检测与可调配置 |
| `drone_sdk` | 设备协议、遥测、最终发送检查、停止、落地确认和 `FrameResult` |

`Application` 注入设备、跟随、视觉和日志；`UnifiedRuntime(device, follow, vision, log, runner=...)` 不创建另一套设备。`FollowService(device, config=...)` 直接使用该 SDK，并持有识别与控制资源。SDK 帧只含 `frame`、`frame_id`、`timestamp`、`stale`，各消费者不另造解码身份。

## 一条真实调用链

以模型要求“查询状态后执行动作”为例：

1. 阿里 `OmniSession` 或豆包 `RTCBridge` 接收工具事件，`app/Tools.py` 校验 `run_program(code)` 并绑定本轮身份。
2. `UnifiedRuntime.run()` 检查运行状态和 generation，创建一次性的 `Host`，让 `SandboxRunner` 启动 Docker。
3. 容器 `worker.py` 执行完整 Python；`drone.get_status()` 等调用写入 RPC，不直接访问设备或密钥。
4. `Host.dispatch()` 校验方法和位置参数。动作先暂停已有跟随并取得本次 SDK 授权，再调用 `device.action_with_grant()`；SDK 在实际发送前复查授权与时效。
5. 程序结果先完成容器与控制权清理；成功且原目标仍有效才按票据恢复跟随。供应商收到对应工具 ID 的结果后再向用户回答；豆包通过服务端 `UpdateVoiceChat` 回传。

一次会话可多次提交程序；每段程序的 Host 与取消事件不复用。`accepted` 仅表示接收请求，最终动作、恢复及清理结果必须分别确认。

## 常见需求改哪里

| 需求 | 首先看 | 验证 |
| --- | --- | --- |
| 改模型提示、工具参数或程序上限 | `app/Tools.py` | `tests/integration`、`tests/rtc` |
| 改凭据、模型或云协议 | `providers/VoiceRoutes.py`、对应供应商客户端 | `tests/ai`、`tests/rtc`；真实云另行显式诊断 |
| 改耳机选择、播放、媒体恢复 | `web/OmniClient.js` / `RTCClient.js` | `npm --prefix web test`，再现场耳机检查 |
| 改动作授权、取消、临时跟随交接 | `app/Runtime.py`、`execution/Host.py` | `tests/integration`、`tests/sdk`，容器改动加 `Check.ps1 -Docker` |
| 改导航视觉 | `navigation/Observation.py`、`vision.py`、`TargetTracker.py` | `tests/ai/VisionTests.py`、`TargetTrackerTests.py`、`tests/integration/NavigationTests.py` |
| 改跟随距离、构图、控制律 | 先 `follow/configs`，需要改算法再进 `follow/r3_follow` | `tests/follow`；RC/高度方向需现场逐轴验证 |
| 改设备发送、遥测或解码 | `drone_sdk/controller.py`、`_driver.py` | `tests/sdk`，再显式硬件诊断 |

## 必须保留的运行边界

控制动作绑定 `control_epoch` / `command_epoch`，同名 owner 不代表同一次授权。取消先同步使旧 generation 失效，再异步停止；迟到线程不能借新任务授权执行。已发 Tello 命令取消/超时后若回复归属不明，会拒绝普通运动与非零 RC，只留归零和优先降落；清空队列或等几秒不能消除这种歧义。

Docker 创建或删除未确认不能报清理成功。Runner 保留容器名称，下一次运行或退出时重试回收；回收仍失败则不再创建新容器，独立的停止/降落入口仍可用。控制权清理未确认时，Runtime 保留待清理 Host 并重试；仍未确认时只允许查询状态、查询跟随状态、停止和降落，不允许普通运动、新跟随或导航。原任务失败不能被后续清理异常覆盖。

`FollowService.stop()` 停运动、作废恢复票据并重置参考，识别线程可继续供状态显示；`close()` 用于退出并确认线程结束。临时接管使用 `pause_if_active()` / `finish_action()`，不能用 stop 冒充暂停。调整跟随参考只在用户明确要求、匹配轴动作完成、后续新帧及整段程序成功时应用。

默认跟随配置是 `aircraft_A_ID0_motion_follow.json`：ID0、自适应横移/转向、relative_size 与上下跟随。横向共享预算 50 RC、前后 40 RC、上下 20 RC；高度 50–200 cm，距边界 20 cm 限制继续外向升降，高度未知/过期则升降归零。短暂丢失默认归零等待最多 30 秒；RC 与标记图像占比均不代表米制测量或物理速度。

导航先要求新鲜源帧并观察多张图的稳定性，这不证明物理悬停。JPEG 只编码一次，供应商只传输；`Vision` 统一解析归一化 xyxy 框。云语义可在有界历史中作为种子，经本地跟踪追到当前帧；语义窗口最多 6 秒、追赶最多 1 秒，最终运动仍检查当前帧时效并在发送前复查。低纹理、歧义、跟踪失效、超窗均拒绝；静止画面的像素相同不等于解码停止，旧帧 ID 也不能刷新时效。解码时间不是相机曝光时间。

阿里音频或图像暂缺只暂停相应媒体并保留会话；不自动换麦克风、耳机、路线或重放旧音频。真正连接故障仍撤销授权。豆包与阿里的本地完整程序预算均为 60000 UTF-8 字节；豆包真实 RTS 数据包另有约束，这不是基础通道可承载 60 KB 的保证。两家模型/endpoint 与当前媒体路线保持原选择，阿里没有迁移至 WebRTC。

退出分别确认停止、落地、图传和云会话；任一关键清理失败会保留失败信息。豆包须停止 Bot 并离开真人房间，阿里关闭云 WebSocket；网页关闭不证明计费已停止。运行日志在 `logs/Runtime`，默认约 16 MB 分段、共享十进制 1 GB 保留预算，只回收本日志器已关闭分段；活动/旧格式日志不自动删除，预算或磁盘错误会报告。
