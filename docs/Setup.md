# 环境、凭据与入口

本页负责安装和配置；运行顺序见 [Flight](Flight.md)，内部职责见 [Architecture](Architecture.md)。以下命令从仓库根目录执行。

## 环境与安装

需要 Windows、uv 管理的 Python 3.11、Node.js 22+、已运行的 Docker Desktop Linux containers。电脑同时连接 Tello Wi-Fi 与互联网，可用独立网络接口或 USB 共享。其他程序/手机 App 不得争用同一飞机或 UDP 端口。

```powershell
uv sync --frozen
npm --prefix web ci --ignore-scripts --no-audit --no-fund
docker build -t companion-sandbox:0.4 execution/sandbox
.\Check.ps1
.\Check.ps1 -Docker
```

默认检查是离线 Python 与 JavaScript；`-Docker` 才实际执行容器项，均不调用收费云或连接飞机。换机重新安装，不复制旧虚拟环境、临时令牌或音频设备索引；依赖由 uv/npm 管理，不向全局 Python 安装。

## 服务端凭据

正式语音读取所选路线的 JSON 文件。长期密钥留在服务端，不进网页、模型程序、Git 或证据。`.env.example` 只是字段模板，不会自动加载，也不是无文件语音启动方式。

| 路线 | 默认文件（相对当前用户目录） | 覆盖方式 |
| --- | --- | --- |
| `alibaba` 默认 | `Desktop/tmp/DomesticAiTrials/AlibabaCredentials.json` | `-AlibabaCredentials` 或 `ALIBABA_CREDENTIALS_FILE` |
| `doubao` 备选 | `Desktop/tmp/DoubaoRealtime/Credentials.json` | `-DoubaoCredentials` 或 `DOUBAO_CREDENTIALS_FILE` |

阿里文件提供 ApiKey、WorkspaceId、WebSocketUrl、Model、Region。默认 Qwen3.8 Omni Flash Realtime；本机观察/问答使用 `AlibabaVisionClient`，无需豆包凭据。豆包文件分别提供 RTC、Seeduplex 和 Ark 字段，本机视觉使用方舟 `ModelClient`。底层字段的用途不可互换：

- `RTC_APP_ID` / `RTC_APP_KEY`：RTC 应用与短期入房 Token。
- `VOLCENGINE_ACCESS_KEY_ID` / `VOLCENGINE_SECRET_ACCESS_KEY`：VoiceChat OpenAPI 签名。
- `VOLC_S2S_APP_ID` / `VOLC_S2S_TOKEN`：Seeduplex。
- `RTC_VLM_MODEL` / `RTC_VLM_API_KEY`：方舟视觉模型与密钥。

旧直连语音的 X-Api-Key 不能替代这些字段。必要配置与 Docker 在连接飞机前预检，预检成功仍不证明真实云可用。模型与 endpoint 保持现有配置；阿里仍经本机 WebSocket 转发 PCM/JPEG，没有迁移至阿里 WebRTC。

服务商可能分别收取语音、视觉、云任务与 RTC 费用；本地不设金额限制，会话时长/轮数/token/消息预算也不代替账单。诊断的真实云请求要求显式 `--real`；先看脚本帮助和账户状态，参数传文件路径，密钥不要直接写命令行。

## 入口与参数

```powershell
.\start_flight.ps1 -DryRun
uv run --frozen python -m app.Application --help
```

启动器只组装并转发参数，Python 负责参数与配置的单点校验。`-DryRun` 仅打印命令，不运行 Python、不读取验证配置、不打开设备或云服务；它不能证明配置正确。需要独立检查跟随文件时，可先查 `uv run --frozen python diagnostics/follow/check_follow_config.py --help`。

现场优先用 `start_flight.ps1`，默认跟随配置位于 `follow/configs`。默认网页 http://127.0.0.1:18848；`-WebPort 18849` 显式改端口，占用时报错，不自动选择其他端口。重启会话后刷新页面取得新授权；设备由用户手动选择，不跨电脑复用索引。

Python 使用三个独立子命令：

```powershell
uv run --frozen python -m app.Application plan --text "查询飞机状态的程序"
uv run --frozen python -m app.Application run --real-drone --program Task.py
uv run --frozen python -m app.Application voice --real-drone --voice-provider alibaba
```

`plan` 调真实文本模型但不连接飞机，只有它接受 `--image`。`run` 的 `--program` 与 `--text` 二选一；文件程序默认不创建视觉客户端，需要时显式加 `--vision`，文字任务使用文本 ModelClient 的视觉能力。文本模型读取 `AI_CHAT_URL`、`AI_API_KEY`、`AI_MODEL` 环境配置。`voice` 才接受供应商凭据、会话时长及网页端口参数。用 `<子命令> --help` 查看对应参数。

现场启动器默认 `voice`；`-Program` / `-Text` 映射到 `run`，`-Text ... -PlanOnly` 映射到 `plan`。静态图片和文件视觉参数使用上述 Python 子命令。默认阿里，启动前用 `-VoiceProvider doubao` 选择备选。失败后先停止、核对飞机，再重启选择路线；不自动切换、重放旧指令或恢复旧授权。没有生产模拟入口或本地 STT/TTS 降级。

豆包备选沿用 OpenAPI `2025-06-01`、`S2SConfig.Provider="volcano_duplex"`、`OutputMode=1`、`VisionConfig.Enable=true`；`ThinkingType="disabled"` 减少额外思考延迟，但不保证时效达标。`LLMConfig.ModelName` 可为模型 ID 或 ep- 接入点，自定义资源另需 APIKey，不改用旧接口的 EndPointId。

协议依据：[阿里实时多模态与工具](https://help.aliyun.com/zh/model-studio/realtime)、[豆包混合编排](https://docs.volcengine.com/docs/real_time_communication/Accessend-to-endreal-timespeechlargemodel?lang=zh)、[LLM 配置](https://docs.volcengine.com/docs/real_time_communication/ConfigureLargeModelLLM?lang=zh)、[工具调用](https://docs.volcengine.com/docs/real_time_communication/FunctionCalling-1?lang=zh)。
