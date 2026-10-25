> 2026-10-24 修复候选：正式入口为 `modules/ai/main.py`。
> 多步程序使用 `--program`（Docker），全双工使用 `--realtime`（独立实时服务配置）。
> `--always-on` 是分段语音兼容路线，不代表全双工。先阅读 [REVIEW_FIXES.md](REVIEW_FIXES.md)。
> 旧版本标签/历史首飞说明不构成当前真机验收通过证明。

\# AI随行小伙伴 - 任务2 SDK



\## 启动步骤

## ⚠️ 请务必使用 Python 3.11 版本运行本项目！

> **原因**：OpenCV / NumPy 对 Python 3.13 的支持尚不完善 —— `numpy==1.26.4` 没有 3.13 的预编译 wheel，而 `opencv-python==4.8.0.76` 又依赖 numpy 1.x 的 ABI。用 3.13 会出现依赖装不上或运行时崩溃。

1\. 安装 Python 3.11（推荐 3.11.9）

2\. 项目根目录运行：`pip install -r requirements.txt`

3\. 运行测试：`python test\_stub.py`

4\. 看到 `\[假飞行]` 相关打印即说明环境正常。



\## 规则

\- 只准改 modules/ 下自己的文件夹。

\- 不准 import djitellopy，只准 `from drone\_sdk.controller import ...`

\- 缺函数在群里说，我来加。



\## 真机地面测试（拆桨叶，不起飞）

> 真飞行尚未授权。下面这套只做地面验证：连真机、看真实画面、点动电机，**脚本里没有任何起飞指令**。

> v2.0 起已具备真机首飞能力（`main.py --real-drone --allow-real-takeoff`，起飞有授权+电量双闸门）。
> **室内首飞请严格按 [FLIGHT_SOP.md](FLIGHT_SOP.md) 执行**：装桨注意事项、双网检查、起飞前喊话、紧急迫降、演示台词。

1\. 拆下全部桨叶，飞机开机，电脑连上 `TELLO-XXXXXX` WiFi。

2\. 先离线自检安全限制（不连飞机，13 项断言）：

&#x20;  `python test\_ground\_real.py --self-check`

3\. 真机地面测试（必须显式确认桨叶已拆下）：

&#x20;  `python test\_ground\_real.py --props-removed`

&#x20;  - 打印真实电量/高度/链路状态，并弹出 OpenCV 实时画面窗口。

&#x20;  - 按键：`w` 提示（标准 Tello 固件不支持 motoron），`s` 急停归零（`rc 0 0 0 0`），`b` 打印状态，`r` 重启视频流，`q`/`ESC` 退出。

&#x20;  - ⚠ 实测 `turn_motor_on`（`motoron`）在标准 Tello 固件上返回 `error`：**电机验证请走 `takeoff`**（`main.py --real-drone --allow-real-takeoff`，桨叶已拆）。

&#x20;  - 只看状态不开画面：加 `--no-video`；定时退出：加 `--seconds 20`。

3.5 现场联调监控（全链路指令流水，带 `[拦截]` 标注）：

&#x20;  `python run\_ground\_test.py --demo --no-video`   假数据跑通监控逻辑，不连飞机

&#x20;  `python run\_ground\_test.py --props-removed`      真机监控（画面 + 电量/高度 + 指令流水）

&#x20;  - 在 `controller` 层装探针：3组 `update\_velocity`、4组 `move\_*`、控制权申请/释放、跟随启停都会打印。

&#x20;  - 任何返回 `ok=False` 的调用都会打印 `[拦截]` + 中文原因（如"未起飞（地面状态）"）。

&#x20;  - 按键：`v` 3组速度演示，`c` 4组动作演示，`d` 释放控制权，`s` 急停归零，`b` 状态，`q` 退出；`w` 只打印"标准 Tello 不支持 motoron"提示（真正验证电机走 `takeoff`）。

&#x20;  - 打印走独立线程、取帧与状态也走后台线程，主线程只跑 OpenCV 窗口；渲染上限 25fps，避免 CPU 打满导致窗口失去响应。

3.6 无人值守自动联调（不弹窗、不按键）：

&#x20;  `python run\_auto\_integration.py --self-test`     离线自检 17 项（含 RGB/BGR 断言）

&#x20;  `python run\_auto\_integration.py --props-removed`  真机自动跑：拦截验证 + 抓帧存 `current\_frame\_fixed.jpg` + 报告 `auto\_integration\_report.json`

3.7 图传诊断（黑屏/无画面时先跑这个）：
&#x20;  `python test\_live\_view.py --self-check`           离线自检 7 项

&#x20;  `python test\_live\_view.py --props-removed`        裸 UDP 抓包探针 + 持续拉流 + 实时窗口

&#x20;  - 先用裸 socket 独占绑定 11111 数包：**0 包 = 网络/防火墙/端口占用**，不是解码问题。

&#x20;  - 再逐帧打印 `shape / mean / 解码线程存活 / 重连次数`，成功才存 `live\_test.jpg`。

&#x20;  - 默认用自研 av 解码后端（读超时 2s、自动重连、错误可见）；失败会自动再试 djitellopy 后端对照。

&#x20;  - djitellopy 收到真实帧前会一直返回 `300x400` 全零占位帧——SDK 已把它判为「无画面」，不会再存黑图。

3.8 终极跨组联调（真机画面 + 3组跟随 + 4组离线 AI，20 秒自动收工）：

&#x20;  `python run\_final\_real\_sim.py --self-check`       离线自检 6 项

&#x20;  `python run\_final\_real\_sim.py --props-removed`     真机联调：ArUco 识别、3组算速、4组指令、拦截流水、FPS

&#x20;  - 真帧持续 `publish\_frame` 进 SDK，3组 `MarkerTracker`/`FollowController` 用的是**同一份真画面**。

&#x20;  - 3组算出的速度走 `update\_velocity`，地面必被 `[拦截] 未起飞（地面状态）`；归零请求按设计放行。

&#x20;  - `start\_follow()` 在地面会被 3组自己的飞行状态门禁挡住（`connected\_airborne\_state\_required`），属安全设计。

&#x20;  - 4组离线 `Demo` 发「前进30厘米」：控制权交接 + 动作同样被 `[拦截]`；到点自动释放、关图传、断开。

&#x20;  - 低电量保护：电量 <20% 会告警（固件低电量会拒指令/强制降落），降到 `--battery-min`（默认 10%）自动安全收工；真机掉线也不抛 traceback，统一走清理分支。

3.9 语音链路自检（有线耳机，不连飞机）：

&#x20;  `python test\_voice\_loop.py --self-check`        离线自检 15 项（不碰声卡、不联网）

&#x20;  `python test\_voice\_loop.py`                     对着耳机说 3 秒，跑完 录音→STT→LLM→TTS

&#x20;  `python test\_voice\_loop.py --text 向前飞 --no-play`  只测文字链路

&#x20;  - 四阶段日志：`[录音]` / `[STT]` / `[LLM]` / `[TTS]`，每步括号里标注来源（API / Mock / SAPI）。

&#x20;  - 未配置 STT → 键盘输入 Mock；未配置 TTS → Windows SAPI 离线合成（零 Key，中文音色取决于系统语音包）。

&#x20;  - ⚠ 若语音服务与聊天不是同一家，**必须显式设置 `AI_STT_KEY` / `AI_TTS_KEY`**，否则会回落到 `AI_API_KEY`（等于把聊天 Key 发给语音服务商）。

&#x20;  - 录音按设备原生采样率（本机耳机 16k/24k/44.1k 实测均可用）。

&#x20;  - STT 优先级：`AI_STT_URL` 接口 → **本地 faster-whisper**（无需 Key）→ 键盘 Mock；本地模型放 `models/whisper/small`（已 gitignore）。

&#x20;  - 本地模型从魔搭下载（huggingface 在当前网络被重置），命令见 `SETUP\_NEW\_PC.md` 第 4 节。

> 新电脑冷启动（Python 版本 / 依赖镜像 / 模型下载 / 音频配置 / 双机注意事项）：见 [SETUP\_NEW\_PC.md](SETUP\_NEW\_PC.md)。

3.10 未知指令安全兜底（听不懂绝不乱动）：

&#x20;  - 规则：听不懂 / 不在动作库 / 大模型输出非法格式 / 代码被白名单拦下 → **不执行任何动作**，统一回复并播报「抱歉，我没听清，请再说一遍」。

&#x20;  - 实现：`ai_agent.PROMPT` 增加硬性约束；`program_runner` 白名单拦截后统一话术；新增 `spoken` 字段专供 TTS（控制台仍保留技术细节）。

&#x20;  - `demo_text.Demo` 的未知指令同样返回该话术，并保证不触碰飞机。

&#x20;  - 回归：`python test\_unknown\_command.py`（7 项，离线，含"模型生成未授权代码"与"多语句"两种攻击面）。

&#x20;  - 要听到播报需配置 `speak_replies: true`；未配置 TTS Key 时自动降级 Windows SAPI（base64 传文本，避免 GBK/UTF-8 乱码）。

3.11 离线语音冒烟（不用麦克风、不发声）：

&#x20;  `python test\_voice\_loop.py --stt-smoke`  —— SAPI 合成「向前飞」→ AGC/VAD → 本地 whisper → 断言识别正确

&#x20;  - 本地 STT 前端：AGC 目标 `-20dBFS`（上限 `+24dB`）、VAD 静音 `300ms`/门限 `0.5`；可用 `AI\_LOCAL\_STT\_AGC`、`AI\_LOCAL\_STT\_AGC\_MAX\_DB`、`AI\_LOCAL\_STT\_VAD\_THRESHOLD` 调整。

3.12 免按键全双工语音（现场演示用，最"省事"的形态）：

&#x20;  `python main.py --live --always-on --config ..\..\config.demo.json`（真机再加 `--real-drone --allow-real-takeoff`）

&#x20;  - **没有 API Key 也能先验耳机**：`python main.py --offline-voice --always-on --config ..\..\config.demo.json`
&#x20;    音频链路是真的（本地 whisper + SAPI 播报），理解走离线样例（起飞/跟着我/降落/还有多少电/前进N厘米）；
&#x20;    配好 `AI_CHAT_URL / AI_API_KEY / AI_MODEL` 后去掉该开关即接真实 DeepSeek。
&#x20;  - 不接语音时提示「需要 --live 或 --offline-voice」，不会静默吞掉 `/voice` 指令。

&#x20;  - 持续监听麦克风，能量 VAD 自动判"开始说话/说完了"，静音 `--silence-s`（默认 1.5 秒）即停录。
&#x20;  - 识别结果**直接执行**，不弹 `y` 确认；单句上限 `--max-utterance`（默认 10 秒），防止环境噪声把录音拖长。
&#x20;  - 防自激：TTS 播报、动作执行、键盘路径手动录音期间一律**关闭采集**（不是清缓冲），播完才重新开麦。
&#x20;  - 识别为空 → 播报「抱歉，我没听清，请再说一遍」后继续监听；说「结束」或输入 `/listen` 关闭。
&#x20;  - 门限自适应：`门限 = max(-55dBFS, 底噪 + 6dB)`，可用 `AI\_VAD\_MIN\_DBFS` / `AI\_VAD\_MARGIN\_DB` 现场微调；日志里的 `[录音] … 自适应门限 -55 dBFS` 就是当前值。
&#x20;  - 采集用 PortAudio **回调**而非阻塞读：Windows WDM-KS 设备不支持阻塞 API（`PaErrorCode -9999`），采样率不支持时自动退回设备原生率（常见 48kHz）。

3.13 三组 Yaw 偏航算法 · 真机地面调试台（拆桨叶）：

&#x20;  `python run\_yaw\_ground\_test.py --props-removed`（离线自检：`--self-check`，11 项，不连飞机不碰声卡）

&#x20;  - 一个进程出两个界面：**窗口1 终端主控台**（每 2 秒真实电量/高度/飞行状态/链路 + 四组 `[STT]`/`[LLM]`/`[TTS]` 日志）、**窗口2 OpenCV 视觉台**（图传 + 三组 `MarkerTracker` 绿色 ArUco 框 + 中心红点 + 左上角 `cx-480` 偏差与 Yaw 值）。
&#x20;  - Yaw 直接用现场配置构造 `FollowController`（默认 `modules/follow/configs/aircraft_A_ID0.json`：`openpose_pid` + `yaw`），换算与 `FollowService.control_once()` 完全一致（yaw 夹紧 ±20 RC），所以画面上的数字就是要下发的偏航量。
&#x20;  - 安全：不含任何起飞指令（`configure_takeoff(authorized=False)` 兜底）；所有非零速度都会打印 `[拦截] 未起飞（地面状态）`；语音「急停/降落」走 priority 通道并打印 `[急停响应]`，地面默认不向飞机真发 `land`（`--allow-real-land` 可强制）。
&#x20;  - 跟随配置优先级：`--follow-config` > `R3_FOLLOW_CONFIG` > 仓库默认配置；ArUco 必须在镜头里**正对**才能完成参考尺寸标定（失败时终端会提示，按 `c` 重标）。
&#x20;  - **速度预算（RC 上限）**：配置字段 `yaw_limit` / `forward_limit` / `max_request_rate`（旧默认 0.20 / 0.30 / 0.8）。`configs/aircraft_A_ID0.json` 现为 **0.50 / 0.40 / 2.0 → ±50 / ±40 RC、200 RC/s 斜率**。服务层钳位由这两个数推导，所以"算法输出上限"和"下发钳位"永远一致；只改钳位是无效的（会被算法夹回去）。⚠️ **RC 100 是满杆（Tello 常态模式约 2.5 m/s），不是 1 m/s**；`forward_limit=0.4` 就是按"室内最高 1 m/s"折算的保守值。升降通道恒为 0（三组从不下达高度指令）。
&#x20;  - **水平增益**：`openpose_control.YAW_GAINS = (0.16, 0, 0.03)`（P 由 0.25 降到 0.16、新增小幅 D，`v3.2-flight-tuned`）。D 有两个保护：不使用短于 20ms 的帧间隔（防抖动放大），且贡献上限 10 RC（防大幅跳变时反向打满）。台架实测：dx→yaw 近似线性（200px→21RC、400px→42RC），饱和点推到 ≈470px 之外。
&#x20;  - 高速度下的安全兜底：速度看门狗 `controller.VELOCITY_WATCHDOG_S = 0.3s`（满杆最坏失控位移约 0.75m，原 0.5s 是 1.25m），独立守护线程；`stop_motion(priority=True)` / `land(priority=True)` 绕过动作锁与代次校验、立即归零。回归见 `modules/follow/tests/test_rc_budget.py`。
&#x20;  - **物理安全兜底（全局组合键，pynput 低级键盘钩子，无需管理员权限）**：`Ctrl+Shift+Alt+A` = 急停/悬停（`stop_motion(priority=True)` + 速度归零）、`Ctrl+Shift+Alt+L` = 降落（`land(priority=True)`）。焦点在 OpenCV 窗口 / 终端 / 其它程序上都生效，触发时打印醒目的 `[物理快捷键] 触发急停/降落`，同一指令 1 秒内只执行一次；缺 `pynput` 时脚本自动 `pip install` 一次，装不上则退回 `s` 键方案（`--no-hotkeys` 可关闭全局组合键）。地面状态下的语音「降落」默认只做零速度，加 `--soft-land`；物理快捷键始终真发 `land`。这套组合键已抽到 `drone_sdk/emergency_hotkeys.py`，**实飞入口 `modules/ai/main.py` 也注册同一组键**（真机走 `session.interrupt`、`--volc-dialog` 走 `dialog.emergency`），`F12`/`F11` 保留为终端热键，两套都可用。

⚠️ 同一时间只能跑一个连飞机的脚本（UDP 8889 独占）。第二个进程会报 `WinError 10048`，先关掉上一个窗口，或：

&#x20;  `Get-NetUDPEndpoint -LocalPort 8889`（查占用进程）

3.14 装桨叶实飞一键启动（语音起飞/跟随 + 全程日志）：

&#x20;  `start_flight_test.cmd`（双击或命令行）→ 先跑飞行前检查，再拉起**窗口1 终端 + 窗口2 图传**。`-DryRun` 只检查不启动，`-Hybrid` 换横移+转向配置，`-Volc` 走豆包全双工（需 `VOLC_API_KEY`，无图传窗口），`-NoVideo` 关画面省 CPU。

&#x20;  - 默认**离线语音**（本地 faster-whisper 识别 + Windows SAPI 播报，不需要任何 API Key），启动参数：`--real-drone --allow-real-takeoff --offline-voice --always-on --show-video --min-battery 10 --config modules/ai/config.flight-test.json --log-file logs/flight_<时间>.log`。
&#x20;  - **起飞电量门限**：默认 **10%**（与 `main.py --min-battery` 的默认值一致，`-MinBattery` 可改）。低于 10% 直接拦；10%~20% 与 20%~50% 只发**黄色警告**——因为 Tello 固件自己在 ≈50% 就回 `error` 拒飞，软件门限压得再低也飞不起来，真正该看的是电量和温度。**温度门禁是硬拦截**（`--temp-max` 默认 75℃）。
&#x20;  - **飞行前检查** `tools/flight_preflight.py`：跟随配置（必须 `openpose_pid`）、whisper 模型是否已缓存、SAPI 是否可用、飞机连通、电量与电池温度。只监听 8890 的状态广播（`bat/templ/temph/h`），**不发 `command`**——用临时端口探测会把 Tello 的 UDP 会话抢走，导致 djitellopy 收不到回复。退出码：0 可飞 / 2 连不上 / 3 电量低于硬门限 / 5 SDK 命令口卡住（需断电重启）/ 6 8889 被占 / 7 电池过热；`WARN=battery_below_firmware_line` 只是提醒不拦。
&#x20;  - **窗口2 HUD 图例**：绿框=ArUco 四角、红点=中心、绿竖线=画面中线；左上角 `found/cx/offset(...px)/sr`、`detect=…ms budget=…`、四通道速度 `v L/F/U/Y`，以及**跟随状态行**：`follow: RUNNING`（绿）/ `follow: STOPPED (原因) -> say it again`（红，需再说一次「开始跟随」）/ `follow: NOT STARTED`（橙）/ `follow: PAUSED`。OpenCV 画不了中文，状态行用 ASCII，中文原因看窗口1。
&#x20;  - **日志**：`logs/flight_<时间>.log`（stdout+stderr 全程，含 STT/动作/RC/三组状态）+ `logs/flight_turns.jsonl`（每轮对话记录）。`logs/` 已在 `.gitignore` 里。
&#x20;  - 离线语音是**固定口令**：起飞 / 跟着我 / 停止跟随 / 停在这里 / **升高 / 降低** / 降落 / 还有多少电。识别结果会先去标点再匹配，`起飞。`、`好的，起飞吧` 也能命中；「停止跟随」优先于「跟随」。
&#x20;  - **起飞后自动补高度**：Tello 起飞只到约 1m，人站着举 ArUco 够不着，所以起飞成功后按真实 `height_cm` 闭环**离散上升**到 `--climb-target-cm`（默认 120cm，`0`/`--no-auto-climb` 关闭；启动器用 `-ClimbCm`）。每步 ≤50cm、8 秒超时、读不到高度或上升被拒就原地放弃；仍受 `_driver` 的 250cm 天花板与 `_plan_ascent` 限幅保护。三组跟随的升降通道**依然是 0**（只由这两个入口改高度，跟随律不受影响）。

4\. 底层硬限制（真机模式生效）：限速 `100 cm/s`（与固件上限一致，室内 1m/s）、限高 `2.5 m`、RC 通道 ±100、地面强制 0 速度、指令超时+断连保护。详见 `API.md` 4.3。

5\. 离线回归仍全部走模拟模式，真机代码只在 `drone\_sdk/\_driver.py` 的 `connect()` 之后生效。

## 场景验收测试与审查证据

行走跟随、红色帐篷、旋转和恢复跟随的43项离线验收，见
[ScenarioAcceptance/README.md](ScenarioAcceptance/README.md)。
原始逐项失败证据与既有测试记录保存在
[ScenarioAcceptance/Evidence](ScenarioAcceptance/Evidence)。
历史记录中的失败对应尚未满足的验收条件；当前默认运行将已知缺口标为
XFAIL，`--strict` 保留原始失败行为。测试入库不代表缺陷已经修复或真机验收通过。

2026-10-24 合入第三组 `r3-b313eb3-fix1` 源码候选：后台默认转向跟随、
大图局部补检、恢复/清理状态与异步会话日志。新增
[R3Tests.py](ScenarioAcceptance/R3Tests.py) 复现3项仍未修复的安全缺口；
详见[合并验证说明](ScenarioAcceptance/README.md#r3-candidate-integration-2026-10-24)。
该候选没有完成实机验收。

