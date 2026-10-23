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



\## 真机地面测试（拆桨叶，不起飞）

> 真飞行尚未授权。下面这套只做地面验证：连真机、看真实画面、点动电机，**脚本里没有任何起飞指令**。

1\. 拆下全部桨叶，飞机开机，电脑连上 `TELLO-XXXXXX` WiFi。

2\. 先离线自检安全限制（不连飞机，13 项断言）：

&#x20;  `python test\_ground\_real.py --self-check`

3\. 真机地面测试（必须显式确认桨叶已拆下）：

&#x20;  `python test\_ground\_real.py --props-removed`

&#x20;  - 打印真实电量/高度/链路状态，并弹出 OpenCV 实时画面窗口。

&#x20;  - 按键：`w` 电机怠速转动，`s` 电机停，`b` 打印状态，`r` 重启视频流，`q`/`ESC` 退出。

&#x20;  - 只看状态不开画面：加 `--no-video`；定时退出：加 `--seconds 20`。

3.5 现场联调监控（全链路指令流水，带 `[拦截]` 标注）：

&#x20;  `python run\_ground\_test.py --demo --no-video`   假数据跑通监控逻辑，不连飞机

&#x20;  `python run\_ground\_test.py --props-removed`      真机监控（画面 + 电量/高度 + 指令流水）

&#x20;  - 在 `controller` 层装探针：3组 `update\_velocity`、4组 `move\_*`、控制权申请/释放、跟随启停都会打印。

&#x20;  - 任何返回 `ok=False` 的调用都会打印 `[拦截]` + 中文原因（如"未起飞（地面状态）"）。

&#x20;  - 按键：`v` 3组速度演示，`c` 4组动作演示，`d` 释放控制权，`w`/`s` 电机怠速开关，`b` 状态，`q` 退出。

4\. 底层硬限制（真机模式生效）：限速 `100 cm/s`（与固件上限一致，室内 1m/s）、限高 `2.5 m`、RC 通道 ±100、地面强制 0 速度、指令超时+断连保护。详见 `API.md` 4.3。

5\. 离线回归仍全部走模拟模式，真机代码只在 `drone\_sdk/\_driver.py` 的 `connect()` 之后生效。

