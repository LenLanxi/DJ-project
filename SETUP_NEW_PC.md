# 新电脑冷启动部署指南（双机联调用）

> 目标：在一台**全新 Windows 电脑**上，从零跑通 2 组 SDK + 3 组跟随 + 4 组 AI 语音，
> 并驱动第二台 Tello。全流程约 20 分钟（不含模型下载）。
> 本文件里的命令都在 **PowerShell** 里执行，工作目录统一为仓库根目录。

---

## 0. 一句话流程

```
Python 3.11 → 建 venv → 装依赖（清华源）→ 下载本地语音模型（魔搭）
→ 生成本机音频配置 → 配 API Key（仅当前终端）→ 跑自检 → 连飞机
```

---

## 1. Python 版本（必须 3.11）

| 项 | 要求 | 说明 |
|---|---|---|
| Python | **3.11.x**（推荐 3.11.9） | 3.13 装不上 `numpy==1.26.4` / `opencv-python==4.8.0.76` 的预编译包；3.10 以下 `StrEnum` 等特性不可用 |
| 位数 | 64 位 | `ctranslate2` / `onnxruntime` / `av` 都只有 64 位 wheel |
| 路径 | 建议 `C:\Python311` | 避免空格与中文路径 |

验证：
```powershell
py -3.11 --version      # 期望 Python 3.11.x
```

安装（如未装）：
```powershell
winget install --id Python.Python.3.11 -e
```

> Git 也需要（拉代码、看历史）：`winget install --id Git.Git -e`

---

## 2. 取代码 + 建虚拟环境

```powershell
cd C:\
git clone <仓库地址> drone-sdk-project
cd drone-sdk-project

py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple
```

---

## 3. 安装依赖（清华镜像）

```powershell
# 1) 主依赖（SDK + OpenCV + 音频）
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 2) 本地语音识别（可选，约 200 MB 依赖）
.\.venv\Scripts\python.exe -m pip install -r requirements-stt.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

- 清华源不通时换阿里云：`-i https://mirrors.aliyun.com/pypi/simple`
- 依赖一律装进 **`.venv`**，不要装到系统 Python（`pip` 前面务必带 `.\.venv\Scripts\python.exe -m`）

验证：
```powershell
.\.venv\Scripts\python.exe -c "import cv2, numpy, djitellopy, sounddevice; print(cv2.__version__, numpy.__version__)"
```

---

## 4. 下载本地语音模型（faster-whisper）

> **重要**：`huggingface.co` 在国内网络的 TLS 会被重置，`hf-mirror.com` 也可能超时。
> 本机实测可用的是 **魔搭 ModelScope**（0.3 秒响应，约 1.2 MB/s）。

模型放到 `models\whisper\small\`（该目录已被 `.gitignore` 忽略，不入库）：

```powershell
$dir = "models\whisper\small"
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$base = "https://www.modelscope.cn/api/v1/models/pengzhendong/faster-whisper-small/repo?Revision=master&FilePath="

foreach ($f in @("config.json", "tokenizer.json", "vocabulary.txt", "model.bin")) {
    Invoke-WebRequest -Uri ($base + $f) -OutFile (Join-Path $dir $f) -TimeoutSec 900 -UseBasicParsing
}
Get-ChildItem $dir | Select-Object Name, @{n='MB';e={[math]::Round($_.Length/1MB,1)}}
# 期望：config.json / tokenizer.json / vocabulary.txt / model.bin(461.1 MB)
```

验证（离线，不联网）：
```powershell
.\.venv\Scripts\python.exe -c "import sys; sys.path.insert(0,'modules/ai'); import local_stt; print(local_stt.describe())"
# 期望：faster-whisper 1.x，模型=small（已缓存）...
```

若想换更小的模型（更快但中文略弱）：
```powershell
# 把 URL 里的 faster-whisper-small 换成 faster-whisper-base，目录改成 models\whisper\base
$env:AI_LOCAL_STT_MODEL = "base"
```

---

## 5. 生成这台电脑的音频设备配置（必须重做）

设备索引是**每台电脑/每次插拔都不同**的，绝对不要从旧电脑拷贝 `audio_config.json`。

```powershell
# 1) 列出所有设备（记下“有线耳机”的输入/输出索引）
.\.venv\Scripts\python.exe test_audio_device.py --list

# 2) 录 3 秒 + 回放自检，并写入配置（推荐同时测底噪）
.\.venv\Scripts\python.exe test_audio_device.py --input-device 4 --output-device 8 --noise-test --save-config audio_config.json
```

- 把 `4` / `8` 换成你 `--list` 里看到的**耳机**索引（不是显示器、不是虚拟声卡）。
- 输出的 `audio_config.json` 记录的是**设备身份**（索引 + 名称 + host API），重插一般不会串；
  换了 USB 口仍可能变，**变了就重跑第 2 步**。
- 该文件已在 `.gitignore` 里，不会进仓库。

验证语音链路：
```powershell
.\.venv\Scripts\python.exe test_voice_loop.py --self-check   # 期望 9/9
.\.venv\Scripts\python.exe test_voice_loop.py                # 说“向前飞”，看四阶段日志
```

---

## 6. API Key（只在当前终端有效，绝不写文件）

```powershell
$env:AI_CHAT_URL = "https://api.deepseek.com/v1/chat/completions"
$env:AI_API_KEY  = "sk-你的DeepSeekKey"
$env:AI_MODEL    = "deepseek-chat"
```

- 语音走本地 faster-whisper + Windows SAPI，**不需要** STT/TTS Key。
- 若改用在线语音服务，必须显式设置 `AI_STT_KEY` / `AI_TTS_KEY`，
  否则会回落到 `AI_API_KEY`（等于把聊天 Key 发给别家）。
- 不要用 `setx`（会持久化到用户环境变量）；用 `$env:`，关窗口即失效。
- **双机请用两个不同 Key**（见第 9 节）。

---

## 7. 防火墙（视频流必需）

无人机把视频**主动**推到本机 UDP 11111，Windows 默认会拦。首次运行脚本时弹窗要勾**专用网络**；漏点过就手动加：

```powershell
New-NetFirewallRule -DisplayName "Tello Video UDP" -Direction Inbound -Protocol UDP -Profile Private `
  -Program "C:\drone-sdk-project\.venv\Scripts\python.exe" -Action Allow
```

> 路径换成这台电脑的真实 venv 路径。

---

## 8. 冷启动自检清单（连飞机前全绿再上）

```powershell
$PY = ".\.venv\Scripts\python.exe"

& $PY test_ground_real.py --self-check        # 期望 13/13（安全限制）
& $PY test_live_view.py --self-check          # 期望 7/7（占位帧/色彩/探针）
& $PY test_voice_loop.py --self-check         # 期望 9/9（语音链路）
& $PY test_voice_loop.py --stt-smoke          # 期望 PASS（SAPI合成→本地识别）
& $PY test_unknown_command.py                 # 期望 7/7（听不懂就问、绝不乱动）
& $PY run_auto_integration.py --self-test     # 期望 17/17（拦截+抓帧）
& $PY run_final_real_sim.py --self-check      # 期望 6/6（跨组联调逻辑）
& $PY test_concurrency.py                     # 期望 3 项（急停 50ms 内）
```

### 8.1 让「听不懂就反问」真正出声

`speak_replies` 默认是 `false`（不播报）。演示前创建一个配置（不入库）：

```powershell
'{"record_seconds": 4, "speak_replies": true}' | Set-Content config.demo.json -Encoding utf8
cd modules\ai
..\..\.venv\Scripts\python.exe main.py --live --config ..\..\config.demo.json --sdk-stub-root ..\..
```

然后把 `config.demo.json` 加进 `.gitignore` 或直接删掉即可（不含密钥，但仍属本机配置）。

真机（拆桨叶）顺序：
```powershell
& $PY test_live_view.py --props-removed        # 图传先通
& $PY run_auto_integration.py --props-removed  # 连接+拦截+画面落盘
& $PY run_final_real_sim.py --props-removed    # 终极联调（20s，含 3 组/4 组）
```

---

## 9. 双机联调（两台电脑 + 两台 Tello）注意事项

1. **各连各的飞机热点**
   - 每台 Tello 的热点名不同（`TELLO-XXXXXX`），一台电脑只连自己那台。
   - 同一台飞机**不能**被两台电脑同时控制（UDP 8889 独占，第二台会 `WinError 10048`）。
2. **不要在同一个热点下演示两台飞机**
   - 两台 Tello 都在 192.168.10.x 网段，若都接到同一个 AP，IP 会冲突。
3. **音频设备配置各自生成**（第 5 节），不要互拷 `audio_config.json`。
4. **DeepSeek Key 分开用**（见下一节限流说明）。
5. 现场 WiFi 干扰：两台飞机尽量隔开 3 米以上，图传卡顿时先看 `test_live_view.py` 的探针包率。

---

## 10. DeepSeek 同一 Key 双机并发的风险

- 单次请求是独立的 HTTP 调用，**两台电脑各自跑进程不会"锁冲突"**；
  唯一共享的资源是**账号级配额与并发限制**。
- 风险点：
  - 触发限流（HTTP **429**），表现为 `RuntimeError('服务HTTP错误429；未重试')`；
  - 项目里的 `http_transport` **明确不重试**（防止重复动作），所以 429 会直接降级为 Mock 回复，
    不会自动补发。
- 建议：
  1. 两台电脑各用一个 Key（同一账号可建多个 Key），便于在控制台分别看用量；
  2. 语音链路本身很轻（一句话一次请求），双机并发一般远低于配额；
  3. 演示时避免"两台同时连续长对话"，把请求错开 1~2 秒即可；
  4. 真被限流时，`test_voice_loop.py` 会打印 `[LLM] 调用失败：服务HTTP错误429；未重试` 并回落 Mock，
     不会让演示崩掉。

---

## 11. 常见坑速查

| 现象 | 原因 | 处理 |
|---|---|---|
| `UnicodeEncodeError: 'gbk'` | 控制台 GBK 打印 emoji/® | `setx PYTHONUTF8 1` 后重开终端 |
| 程序"卡死"、按键失效 | 控制台 QuickEdit 被点中 | 脚本已自动关闭；仍异常就重开终端 |
| `WinError 10048` | UDP 8889 被上一个窗口占用 | 关旧窗口，`Get-NetUDPEndpoint -LocalPort 8889` |
| 图传全黑 `(300,400,3)` | djitellopy 占位帧 / 包没到 | `test_live_view.py --props-removed` 看探针包率 |
| 模型下载卡住 | `huggingface.co` TLS 被重置 / `hf-mirror` 超时 | 用第 4 节 ModelScope 命令 |
| `Invalid sample rate` | 设备不支持 16 kHz | 用 `test_voice_loop.py`（自动用设备原生采样率） |
| 语音识别出繁体 | whisper 偶发输出繁体 | `local_stt` 已做繁→简兜底（起飞/電/飛…） |
| 语音链路只有 Mock | 没配 STT 且未装 faster-whisper | 第 3、4 节 |

---

## 12. 目录结构（部署后应有的样子）

```
drone-sdk-project\
├─ .venv\                     虚拟环境（不入库）
├─ models\whisper\small\      本地 STT 模型（不入库，约 461 MB）
├─ audio_config.json          本机音频设备配置（不入库，每台机器各一份）
├─ drone_sdk\                 2 组底层 SDK
├─ modules\ai\                4 组 AI（含 local_stt.py）
├─ modules\follow\            3 组跟随
└─ test_*.py / run_*.py       自检与联调脚本
```
