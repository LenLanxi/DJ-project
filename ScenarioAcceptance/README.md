# 验收证据与当前测试

当前测试入口：仓库根目录执行 `uv run --frozen pytest -q`。真实容器项需显式设置 `RUN_DOCKER_TESTS=1`；RTC 服务端另运行 `npm --prefix modules/rtc test`。

`Evidence/RealtimeComparison` 是首轮豆包 RTC、阿里实时多模态和腾讯 TRTC 的独立比较。Fixtures.json 固定同一批合成音频/红蓝帐篷图；*Media.json 保留开发与正式尝试、语义校验、真实媒体计时及清理结果；Summary.json 由 `tests/CompareRealtime.py` 离线生成。缺失、失败、仅控制面成功的路线不能被算成完整成功；未做真实耳机、图传或飞行验收。SDK/短期Token/原始输出PCM不入库，相关Setup.json记录来源与复现要求。

`Evidence/RouteSelection20261027` 单独保存后续归因实验：Screen（VAD×发图方式）、Endpoint（完整断句）、Confirm（视觉与容器工具重复，含故障）、Doubao（官方防抢话参数试验）、Speech（三路新语音样本）、Vad（腾讯断句取舍）、Postfix（取消保护后工具验证）。Analysis.json 通过 `uv run --frozen python tests/RouteAnalysis.py` 离线生成，严格区分完整输入、正确内容和媒体送达。保留超时与失败，不把调试样本并入同一速度排名，不把云端配置接收成功当作参数实际生效。原有 HTML 报告和首轮原始记录不覆盖。

`ScenarioTests.py` 仅转发当前 pytest，不再加载已删除的 Agent/Demo/Session/docs 生产路线，也不会把已知失败转换为通过。

`Evidence/R3Integration` 保存本次三组集成回归、配置/图像/交接检查及其范围；`Evidence/Current` 保留前次 RTC 重构的检查、云语音诊断和费用记录。历史 `Cases.json`、`Evidence/History`、`Results` 保留其原始版本和发现；其中的文件名、行号和通过数量可能对应已删除实现，不代表当前状态。

迁移对应：SDK 电量/温度/遥测/降落/排队发送 → tests/SafetyTests.py、SDKProtocolTests.py、AcceptanceTests.py；控制交接 → GatewayTests.py、FollowSafetyTests.py 及 modules/follow/tests；视觉/帧 → VisionTests.py、FrameTests.py；容器隔离/取消 → SandboxSafetyTests.py、ContainerWireTests.py；模型与网络 → TransportTests.py；正式运行生命周期 → RuntimeBoundaryTests.py 等；RTC → RTCBridgeTests.py 与 modules/rtc 的测试。

旧单调用 AST 白名单、固定口令、STT multipart、本地 TTS 与重复入口专用测试随对应功能退役。完整 Python 的替代边界是容器隔离，有价值的控制与失败断言继续保留。
