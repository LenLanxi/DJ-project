# 环境、凭据与入口

以下命令从仓库根目录执行；现场流程见 [Flight](Flight.md)。

## 安装

使用 Windows、uv 管理的 Python 3.11、Node.js 22+。Node 运行本机 RTC 服务，浏览器使用官方 RTC SDK 传输音视频。电脑同时连接 Tello Wi-Fi 和互联网；其他程序／手机 App 不得争用飞机或 UDP 端口。

```powershell
uv sync --frozen
npm --prefix web ci --ignore-scripts --no-audit --no-fund
.\Check.ps1
```

Python 的方舟请求使用官方 volcengine-ark-runtime（导入名 volcenginesdkarkruntime）；网页使用 @volcengine/rtc，Node 使用 @volcengine/openapi 签名调用云端。依赖由 uv.lock 与 web/package-lock.json 固定，启动时不自动安装。

换机重新安装依赖，不复制旧虚拟环境、临时授权和音频设备索引。Check 不访问收费云和真实设备；不需要 Docker 或 WSL。

## 豆包凭据

豆包 RTC 是唯一正式路线，没有阿里备选或自动切换。凭据 JSON 包含以下字段组，填入本账号真实配置：

| 分组 | 字段 | 用途 |
| --- | --- | --- |
| RTC | AppId、AppKey | RTC 房间和临时 token |
| OpenAPI | AccessKeyId、SecretAccessKey | 云端 VoiceChat 任务签名 |
| Speech | AppId、AccessToken | volcano_duplex 原生实时语音 |
| Ark | ModelName、APIKey | 混合编排中的视觉／工具模型，以及独立文字计划和图片请求 |

本机默认文件：

```text
C:\Users\bente\Desktop\tmp\DoubaoRealtime\Credentials.json
```

PowerShell 启动器使用 `-DoubaoCredentials`，Python 使用 `--doubao-credentials`；也可设置 DOUBAO_CREDENTIALS_FILE。`.env.example` 只列出环境配置，不会自动加载。具体默认路径以启动器和 `providers/DoubaoConfig.py` 为准。

长期密钥只留在服务端，不进入网页、模型动作、Git 或证据。网页取得的是有期限的房间 token。配置和 Node 依赖在连接飞机前检查；本地预检通过不证明账号权限、云模型或媒体链路可用。

## 云端能力与容量

StartVoiceChat 使用 `S2SConfig.Provider=volcano_duplex` 与 `LLMConfig.Mode=ArkV3` 混合编排：语音送给原生实时语音服务，方舟模型处理视觉和工具请求。结构化工具动作由方舟模型生成。持续无人机视频由 RTC SDK 发布，导航的独立 `describe`／`observe` 请求使用官方 Ark SDK。

结构化动作参数较短，不再传输 Python 源码；RTC 账号消息容量仍需符合服务要求，真实云联调需核对工具往返。

混合编排配置使用 `<NOVISION>` 规则选择回答来源；普通对话且不需画面或工具时输出此标记，需要看图或工具时不得输出。该规则由服务配置提供。

## 入口

```powershell
.\start_flight.ps1 -DryRun
uv run --frozen python -m app.Application plan --text "查询飞机状态" --doubao-credentials C:\Path\Credentials.json
uv run --frozen python -m app.Application run --real-drone --action-file Task.json
uv run --frozen python -m app.Application voice --real-drone --doubao-credentials C:\Path\Credentials.json
```

- plan：调用方舟模型生成单个结构化动作，不连接飞机；可用 `--image` 提供静态图片。
- run：`--action-file` 与 `--text` 二选一。动作文件默认不创建视觉客户端，需要时加 `--vision`；文字任务使用方舟模型生成单个结构化动作。
- voice：模型语音和无人机视频经 RTC 传输，麦克风默认静音，点击开始说话、说完发送并静音，支持多轮动作执行；本地不等待最终 ASR 字幕才接受工具，普通插话不关闭 RTC 会话。

启动器只转发参数，Python 负责校验。`-DryRun` 不运行 Python、不验证配置、不打开资源。`-ActionFile` / `-Text` 映射到 run，`-Text ... -PlanOnly` 映射到 plan。

默认跟随配置为 follow/configs/aircraft_A_ID0_motion_follow.json。算法固定为 adaptive、relative_size、responsive_v1；可调整数值参数，其他候选算法和米制标定入口已移除。

网页默认 http://127.0.0.1:18848；用 `-WebPort` 显式修改，占用时失败，不自动换端口。会话重启后刷新页面，重新选择麦克风和耳机。没有生产模拟入口或本地语音降级。

云端调用可能计费，本地不设金额限制。真实云诊断须显式授权；把凭据路径而非密钥传给命令。
