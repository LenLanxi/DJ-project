# 验收证据与当前测试

当前测试入口：仓库根目录执行 `uv run --frozen pytest -q`。真实容器项需显式设置 `RUN_DOCKER_TESTS=1`；RTC 服务端另运行 `npm --prefix modules/rtc test`。

`ScenarioTests.py` 仅转发当前 pytest，不再加载已删除的 Agent/Demo/Session/docs 生产路线，也不会把已知失败转换为通过。

`Evidence/Current` 保存本轮软件检查、直连云语音诊断和费用估算，并明确列出未验收范围。历史 `Cases.json`、`Evidence/History`、`Results` 保留其原始版本和发现；其中的文件名、行号和通过数量可能对应已删除实现，不代表当前状态。

迁移对应：SDK 电量/温度/遥测/降落/排队发送 → tests/SafetyTests.py、SDKProtocolTests.py、AcceptanceTests.py；控制交接 → GatewayTests.py、FollowSafetyTests.py 及 modules/follow/tests；视觉/帧 → VisionTests.py、FrameTests.py；容器隔离/取消 → SandboxSafetyTests.py、ContainerWireTests.py；模型与网络 → TransportTests.py；正式运行生命周期 → RuntimeBoundaryTests.py 等；RTC → RTCBridgeTests.py 与 modules/rtc 的测试。

旧单调用 AST 白名单、固定口令、STT multipart、本地 TTS 与重复入口专用测试随对应功能退役。完整 Python 的替代边界是容器隔离，有价值的控制与失败断言继续保留。
