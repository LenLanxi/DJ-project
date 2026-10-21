# 从这里开始

在仓库根目录使用 Python 3.11。

离线演示（无需无人机、依赖或 API）：
uv run --no-project --python 3.11 python -m modules.AICompanion.demo_ai --scenario

统一离线终端：
uv run --no-project --python 3.11 python -m modules.AICompanion.main

公共 SDK 桩联调：
uv run --no-project --python 3.11 --with-requirements requirements.txt python -m modules.AICompanion.SdkDemo --scenario

完整说明见 README.md；已知接口限制见 HANDOFF.md。
