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

