# AI 随行小伙伴：4 组模块 v0.3

本目录来自 AI_companion_v0_3.zip，按上游模块隔离规则整理。所有命令均在仓库根目录执行，使用 Python 3.11。
公共文件 drone_sdk/、根 README/API/requirements 和 test_stub.py 均未修改。

## 两个入口

### 完全离线开发

```powershell
uv run --no-project --python 3.11 python -m modules.AICompanion.demo_ai --scenario
uv run --no-project --python 3.11 python -m modules.AICompanion.main
```

此入口仅依赖标准库，使用本组 MockDrone，不导入公共 SDK、不创建无人机连接、不调用付费 API。
输入：还有多少电、起飞、跟着我、向前走30厘米、降落。
终端支持 /status、/stop、/land、/reset、/mute、/quit。
Windows 也可运行本目录 start_windows.bat，脚本会切换到仓库根目录。

### 公共 SDK 同步联调

```powershell
uv run --no-project --python 3.11 --with-requirements requirements.txt python -m modules.AICompanion.SdkDemo --scenario
```

SdkRunner 仅从 drone_sdk.controller 导入公共方法，不导入 DJITelloPy，不建立第二个无人机连接。
它转换 contract.py 的 dataclass 返回结果，读取状态无需控制权；动作申请 model 控制权并在结束或异常后释放。
模型内部的 drone.get_battery() 是本组白名单语法，实际映射到公共 get_status().battery，并非假设 SDK 有同名函数。

当前上游 aa40228 的公共 SDK 仍是假飞行桩；联调通过不能证明真实飞行正常。
本入口只运行固定离线脚本，同步调用状态、起飞、前进30厘米和降落。
跟随控制权交接、连续速度及真实阻塞中断尚未接通，相关跟随请求明确返回失败。
若已有跟随状态或 follow 控制者，适配器拒绝假设接管成功。停止/降落的跟随接管也尚不可用。
Session 不接受 SdkRunner，以免把仅适用于即时 Mock 调用的锁范围用于阻塞真机动作。
后续若公共 SDK 改为真机驱动，必须先完成交接和中断联调，再开放真实控制入口。

## 计划与反馈

模型只能输出一个白名单调用，例如 drone.takeoff()、drone.move_forward(30)。使用 AST 检查，不执行 eval/exec。
暂不支持任意 Python、循环、多步动作或其他距离。
accepted 仅表示程序格式获准；SDK 动作结果保留 ok/status/reason/is_blocking。
SDK 未提供模拟标志，因此适配结果用 backend="sdk"、simulated=null，不能据此判定是否物理飞行。
不将 accepted 状态当作 completed，不自动重试结果不确定的动作。
模型对话保留最近 6 轮及执行反馈；视觉问答不会解析成动作。

## 模型、语音与视觉（可选）

默认离线脚本不是 AI。只有原终端使用 --live 时才会访问配置的真实服务，飞机仍为本组 MockDrone。
API 采用 Chat Completions 和兼容音频请求格式，不保证所有供应商支持相同能力。

| 环境变量 | 内容 |
|---|---|
| AI_CHAT_URL | 完整 HTTPS 聊天接口地址 |
| AI_API_KEY | 聊天密钥 |
| AI_MODEL | 模型标识 |
| AI_STT_URL / AI_STT_MODEL | 转写地址、模型 |
| AI_STT_KEY | 可选，未设置时使用 AI_API_KEY |
| AI_TTS_URL / AI_TTS_MODEL / AI_TTS_VOICE | 合成地址、模型、声音 |
| AI_TTS_KEY | 可选，未设置时使用 AI_API_KEY |

密钥只放环境变量，不写入代码或提交。真实请求可能收费，程序不自动重试，不推算费用。
原始响应提供的 usage 会记录在 planner_usage。

```powershell
uv run --no-project --python 3.11 python -m modules.AICompanion.main --live
uv run --no-project --python 3.11 --with-requirements modules/AICompanion/requirements-audio.txt python -m modules.AICompanion.main --devices
uv run --no-project --python 3.11 --with-requirements modules/AICompanion/requirements-audio.txt python -m modules.AICompanion.main --live --config modules/AICompanion/config.example.json
uv run --no-project --python 3.11 python -m modules.AICompanion.main --live --image test.jpg
```

复制 config.example.json 为同目录 config.local.json 后可配置音频设备、录音时长和 speak_replies。
/voice 固定时长录音，转写确认后才提交；/mute 停止播报。
录音及转写期间终端不接收新命令，尚无全双工语音，不能用于真机唯一停止通道。
本地图片仅支持 PNG/JPEG、最大 5MB，会上传给配置的视觉模型；始终标记为测试图片。
/image 图片路径 | 问题 可显式看图。视觉答案不会触发动作。
公共 SDK 画面的浅层转换保留原数组，时效和 frame_id 校验函数可复用；实时画面尚未接入终端。
可选 requirements-vision.txt 与根依赖使用同一 opencv-python 和 NumPy 版本，避免同时安装 headless/常规 OpenCV。

## 测试与诊断

```powershell
uv run --no-project --python 3.11 python -B -m unittest -v modules.AICompanion.test_agent modules.AICompanion.test_demo modules.AICompanion.test_independent
uv run --no-project --python 3.11 --with-requirements requirements.txt python -B -m unittest -v modules.AICompanion.test_agent modules.AICompanion.test_demo modules.AICompanion.test_independent modules.AICompanion.TestSdk
uv run --no-project --python 3.11 python -m modules.AICompanion.diagnose
```

原有 25 项离线测试保留，新增 13 项公共 SDK 合同及失败路径测试。
本目录采用命名空间包，使用上述完整模块名运行；不要直接执行内部 .py 文件或依赖根 unittest 自动发现。
Sdk 测试中的实际动作仅适用于当前已确认的假飞行桩，公共 SDK 接入真机后应替换为隔离测试。
实际执行记录见 TEST_RESULTS.txt，接口对接边界见 HANDOFF.md。

## 日志与文件

默认不保存日志。启用时建议 log_path="modules/AICompanion/logs/session.jsonl"，内容包括输入、反馈、计划和耗时。
音频与图片不自动保存；文字日志含用户内容。
本目录 .gitignore 忽略本地配置、日志、WAV 和 Python 缓存。
