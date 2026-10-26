# 统一模型执行层

唯一正式入口为本目录 `main.py`，语音与视频使用 `../rtc` 的 RTC 混合编排。完整 Python 仅在容器执行，通过 Gateway 和 SDKBackend 调用飞机。

安装、凭据、启动、测试和限制统一见仓库根目录 README.md；接口见根目录 API.md。测试替身集中在 tests，不提供正式模拟或离线语音回退。
