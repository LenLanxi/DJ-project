# AI随行小伙伴 · 4组独立开发包 v0.3

**这是4组模拟开发包版本，不是2组正式接口版本。真实无人机连接始终禁用。**

## 先看效果

Python 3.10+，无需安装依赖即可跑离线演示：

```bash
python demo_ai.py --scenario
python main.py
```

Windows可以双击 `start_windows.bat`。默认仅有离线样例脚本，不是真实AI。
输入：`还有多少电`、`起飞`、`跟着我`、`向前走30厘米`、`降落`。
统一入口支持 `/status`、`/stop`、`/land`、`/reset`、`/quit`。
离线样例与模型输出分开标明；所有动作结果均标记模拟。

## 能力及验证边界

| 模块 | 已写的能力 | 本次验证 |
|---|---|---|
| 意图与程序 | 真实模型客户端、单调用Python子集、AST白名单检查 | 离线与替身服务验证；未调用付费服务 |
| 结果反馈 | 取执行器结果，记录计划/结果/上下文 | 模拟验证 |
| 对话上下文 | 最近6轮文本与真实反馈，支持清空 | 离线验证 |
| 语音 | 固定时长录音、上传转写、确认后处理、生成WAV、耳机播放 | 请求格式/音频编码/链路替身验证；未测麦克风及真实语音API |
| 视觉 | 本地图片问答、由模型意图触发读图 | 替身验证；未测真实视觉模型 |
| 画面检查 | 0.5秒时效、frame_id去重、stale与空帧处理 | 元数据测试；未接无人机画面 |
| 中断 | 独立线程处理停止/降落，废弃迟到模型动作 | 模拟验证；未验证实机中断阻塞动作 |
| 启动与记录 | 统一终端、设备列表、配置、可选JSONL、计时、模型token用量 | 本地验证 |

`accepted=true`只表示程序格式获准，**不代表动作成功**。最终结果看执行反馈。
当前编程子集每轮一个调用，前进仅支持30厘米。不支持循环、任意Python和多步任务。
本开发包没有持续速度发送循环，不会臆造2组update_velocity签名。
当前没有自动打断播报的全双工语音；通过 `/mute` 停止播报，文字 `/stop` 和 `/land` 走独立通道。

## 配置模型与语音

由使用者在自己的电脑环境变量中配置。不同供应商的聊天、视觉、转写、合成能力可能不相同。
接口格式采用Chat Completions及OpenAI兼容音频格式；**不保证所有供应商兼容**。

| 环境变量 | 内容 |
|---|---|
| AI_CHAT_URL | 完整HTTPS聊天接口地址 |
| AI_API_KEY | 聊天服务密钥 |
| AI_MODEL | 支持聊天/所需视觉的模型标识 |
| AI_STT_URL | 完整HTTPS语音转写接口地址 |
| AI_STT_MODEL | 转写模型标识 |
| AI_STT_KEY | 可选，转写密钥；不填时沿用AI_API_KEY |
| AI_TTS_URL | 完整HTTPS语音合成接口地址 |
| AI_TTS_MODEL | 合成模型标识 |
| AI_TTS_VOICE | 供应商支持的声音标识 |
| AI_TTS_KEY | 可选，合成密钥；不填时沿用AI_API_KEY |

不要在群里发密钥，也不要把密钥放进代码包。程序不打印密钥。
程序仅在 `--live` 及具体请求时使用API；不会自动重试。转写、聊天、看图、合成可分别产生费用。
模型usage按供应商返回记录；**没有定价配置，不会虚构人民币成本**。

PowerShell设置形式（替换为实际值）：

```powershell
$env:AI_CHAT_URL="https://实际服务/v1/chat/completions"
$env:AI_API_KEY="自己的密钥"
$env:AI_MODEL="实际模型标识"
python main.py --live
```

Linux/macOS使用 `export AI_CHAT_URL='...'` 等方式设置。

## 语音操作

```bash
python -m pip install -r requirements-audio.txt
python main.py --devices
python main.py --live --config config.example.json
```

1. 用设备列表确认耳机输入/输出编号，需要时复制配置为 `config.local.json` 修改编号。
2. 配置语音服务环境变量。
3. 输入 `/voice`，按回车开始固定时长录音（默认5秒）。
4. 检查屏幕转写，输入 `y` 才提交；取消不调用模型和动作。
5. 如需朗读回复，把配置中的 `speak_replies` 改为 `true`。声音由AI合成。
6. 输入 `/mute` 取消当前播放；`/stop` 停止模拟运动；`/land` 模拟降落。

确认转写是本阶段避免误识别的体验安排。后续实际测试后再决定是否减少确认。
部分Linux系统需单独安装PortAudio；未识别到输入设备时先处理系统权限和音频驱动。
录音和语音播放不并行；录音期间、转写请求期间终端不会接收新的命令。
这些限制意味着当前**不能把语音当作实机唯一停止渠道**。
尚不具备连续免按键收音、自动语音端点检测及全双工对话；这些属于体验优化，不依赖他组，但需真实耳机与API测量后再选择方案。

## 视觉问答

```bash
python main.py --live --image test.jpg
```

此时文字或语音提出“看看画面里是什么”，模型可选择视觉问答分支。来源始终标记为本地测试图片。
也可显式输入 `/image test.jpg | 这里有什么？`。
只支持PNG/JPEG，文件不超过5MB；问答会上传指定图片。
视觉答案只作回答，不解析为动作程序。没有实时摄像头时不声称看到了现场。

未来若接OpenCV画面，安装 `requirements-vision.txt`。OpenCV `imencode`输入BGR，输出JPEG的颜色解释正确；不能额外当RGB误编码。

## 日志、计时与诊断

`config.example.json` 默认不存日志。需要时设置 `log_path` 为 `logs/session.jsonl`。
日志包含用户输入、计划、执行结果、模拟状态、回复及耗时；聊天使用量在 `planner_usage`。
统一入口计时包括模型及执行；`pipeline.py`为可复用分阶段语音计时组件。
图片和录音只用于请求，不自动落盘；日志会保存文字内容。
语音播放失败不重复执行动作，文字结果仍保留。

```bash
python diagnose.py
python -m unittest -v
```

本地诊断不联网，只报告配置是否存在；不能代替真实API/耳机测试。

## 需要2、3组交付后才能完成的工作

1. 2组仓库地址、提交号、接口源码/API.md、`test_stub.py`、最小调用示例及运行环境。
2. 确认导入/对象创建、owner传参、返回结构、status枚举、异常和线程并发语义。
3. 将4组临时模拟器转换为2组统一入口，使用同一无人机连接。
4. 3组真实跟随启停与恢复条件，2组统一包装调用。
5. 实测阻塞动作中独立线程stop_motion/land可中断，速度归零和看门狗正常运行。
6. 接真实帧及状态后开展语音—看图—动作整机联调。

**不能把临时模拟器直接换一个类名用到实机。** 当前Runner只允许MockDrone。
`session.py`把检查及即时模拟动作放在同一个锁内；真实阻塞动作必须依据2组中断协议重新接入，不能照搬此锁范围。
未拿到2组签名前不会猜测request_control、update_velocity及跟随函数的完整参数。

## 组内交付

- `main.py`：统一启动、语音、文字、视觉入口。
- `ai_agent.py`、`program_runner.py`：模型计划、受限程序检查执行与反馈。
- `session.py`：普通任务与优先中断的模拟调度。
- `audio_io.py`、`voice_service.py`、`pipeline.py`：音频与语音流水线。
- `local_stt.py`、`offline_tts.py`：本地 faster-whisper 识别与 Windows SAPI 播报（免 Key 降级）。
- `duplex_audio.py`：双工采集/播放底层（已从 `docs/v4-reference` 迁入，供 `test_audio_device.py` 使用）。
- `vision_service.py`：测试图片、帧元数据检查和BGR编码。
- `settings.py`、`diagnose.py`：配置与本地诊断。
- `HANDOFF.md`：已确认约定与未确定签名，供技术成员对接。
- 旧 `demo_text.py`、`demo_ai.py`、`demo_vision.py` 保留为独立模块演示。

接口参考（2026-10-21核对）：
https://developers.openai.com/api/reference/resources/chat
https://developers.openai.com/api/reference/resources/audio/subresources/transcriptions/methods/create
https://developers.openai.com/api/reference/resources/audio/subresources/speech/methods/create
https://python-sounddevice.readthedocs.io/en/latest/api/convenience-functions.html

## 2组（总装与底层）接入层：实飞入口的额外参数

四组交付的 `main.py` 是模拟包入口；接真实 Tello 时由 2组 在其上补了一层（全量替换后需重新补回，已固化为默认行为）：

| 参数 | 作用 |
|---|---|
| `--real-drone --allow-real-takeoff` | 连接真机；起飞默认拒绝，两个参数都加才放行 |
| `--min-battery 10` | 真机起飞电量门限（与 `controller.configure_takeoff` 一致） |
| `--offline-voice` | 本地 faster-whisper 识别 + Windows SAPI 播报，免 API Key |
| `--log-file <path>` | 控制台输出与报错同时落盘 |
| `--show-video` | 图传窗口：绿框标 ArUco、红点标中心，叠加偏差/速度/跟随状态（守护线程，异常自愈成仅终端输出） |
| `--volc-dialog` | 豆包全双工实时语音（火山 Seeduplex）：ASR/对话/TTS 都在服务端，鉴权 `X-Api-Key`，需 `VOLC_API_KEY`；必须显式 `--input-device/--output-device`（`DuplexAudio` 拒绝系统默认） |

豆包模式的三个实测要点：
- **没有 docker 时退到本地白名单执行器**：`program_runner.Runner` 只接受单条 `drone.xxx()` 调用（无循环/无 import），动作照样走 `controller` 安全锁 —— 所以地面阶段能把"尚未起飞"这类拦截演示出来；装了 docker 则走沙箱跑完整程序
- **状态类口令**走 `get_status` 工具（直读 backend/drone，毫秒级）
- **回音**：默认播报期间不上行麦克风（外放时扬声器声音会被自己当指令）。想保留"说话打断我"就设 `VOLC_BARGE_IN=1`，此时必须戴耳机
- **急停**：`interrupt` 工具走 `backend.emergency()` priority 通道，与快捷键同一条路
| `--climb-target-cm 120` / `--no-auto-climb` | 起飞后自动补高度（Tello 自身只到约 1m，人站着举码够不着） |

急停（任何窗口聚焦都有效）：`Ctrl+Shift+Alt+A` = 零速度悬停，`Ctrl+Shift+Alt+L` = 立即降落。
两者都走 `controller` 的 priority 通道，不排动作队列。终端里 `F12` / `F11` 为备用。

HUD 与跟随状态行在独立模块 `follow_hud.py`（`follow_status_text` / `draw_follow_overlay` / `follow_snapshot` / `follow_line`），
不再放在 `main.py` 里 —— 四组整包覆盖会把它一起带走。

自检脚本：

```bash
python test_bluetooth_ai.py     # 录音→STT→豆包→TTS→丢帧防线，不连飞机（需 DOUBAO_API_KEY）
python -m unittest discover -s modules/ai -p "test_*.py"
```

### 语音地面测试（桨叶拔掉，连真机但不授权起飞）

`start_voice_ground_test.cmd` 是专用入口：连真机、读真实电量/高度，但**启动参数里永远没有
`--allow-real-takeoff`**，所以语音说"起飞"会被拒绝，动作指令会被底层以 `尚未起飞` 拦下。

```bat
.\start_voice_ground_test.cmd -ListDevices                 :: 连蓝牙耳机后先看设备号
.\start_voice_ground_test.cmd -MicTest -InputDevice 5      :: 先真录 3 秒确认麦克风能收到声音
.\start_voice_ground_test.cmd -Loopback                    :: 语音自环：合成→识别→口令映射（不用说话）
.\start_voice_ground_test.cmd -NoDrone                     :: 只验语音：模拟飞机，不连真机
.\start_voice_ground_test.cmd -Volc                        :: 豆包全双工语音（需 VOLC_API_KEY，自动挑蓝牙耳机）
.\start_voice_ground_test.cmd -Volc -NoDrone -InputDevice 1 -OutputDevice 3   :: 不戴耳机先验豆包
.\start_voice_ground_test.cmd -InputDevice 5 -OutputDevice 7
.\start_voice_ground_test.cmd -Video                       :: 顺带弹出图传 HUD
.\start_voice_ground_test.cmd -Realtime                    :: 走四组全双工链路（需 AI_REALTIME_* 环境变量）
```

> 注意：Tello 的 8889 是单会话（先发 command 的端口会锁住回包），所以"终端 + 图传"必须是**同一个进程**的两个窗口，不能开两个程序。

豆包模式加 `-Video` 同样会弹图传窗口（`run_modern` 里挂了同一个守护线程查看器）：

```bat
.\start_voice_ground_test.cmd -Volc -Video          :: 终端 + 图传双窗口，语音走豆包
.\start_voice_ground_test.cmd -Video                :: 双窗口，语音走离线 whisper + SAPI
```

窗口里画的是：绿框标 ArUco、红点标中心、X 轴偏差/尺寸比、L/F/U/Y 四通道速度、跟随状态；
画面线程只读不控，异常自愈成"仅终端输出"。

语音口令（离线模式，归一化后匹配，带标点也能命中）：还有多少电 / 现在什么状态 / 现在多高 /
起飞 / 跟着我 / 停止跟随 / 停在这里 / 升高 / 降低 / 向前的距离 / 降落 / 结束。

`tools/voice_ground_check.py` 负责前置自检（设备、本地 STT、SAPI、pynput、入口参数），
`test_voice_ground_launcher.py` 是它的回归护栏：入口参数被整包覆盖删掉时、或启动器哪天
不小心带上起飞授权时，单测会直接失败。