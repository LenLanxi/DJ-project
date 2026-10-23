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

&#x20;  - 打印走独立线程、取帧与状态也走后台线程，主线程只跑 OpenCV 窗口；渲染上限 25fps，避免 CPU 打满导致窗口失去响应。

3.6 无人值守自动联调（不弹窗、不按键）：

&#x20;  `python run\_auto\_integration.py --self-test`     离线自检 17 项（含 RGB/BGR 断言）

&#x20;  `python run\_auto\_integration.py --props-removed`  真机自动跑：拦截验证 + 抓帧存 `current\_frame\_fixed.jpg` + 报告 `auto\_integration\_report.json`

3.7 图传诊断（黑屏/无画面时先跑这个）：
&#x20;  `python test\_live\_view.py --self-check`           离线自检 7 项

&#x20;  `python test\_live\_view.py --props-removed`        裸 UDP 抓包探针 + 持续拉流 + 实时窗口

&#x20;  - 先用裸 socket 独占绑定 11111 数包：**0 包 = 网络/防火墙/端口占用**，不是解码问题。

&#x20;  - 再逐帧打印 `shape / mean / 解码线程存活 / 重连次数`，成功才存 `live\_test.jpg`。

&#x20;  - 默认用自研 av 解码后端（读超时 2s、自动重连、错误可见）；失败会自动再试 djitellopy 后端对照。

&#x20;  - djitellopy 收到真实帧前会一直返回 `300x400` 全零占位帧——SDK 已把它判为「无画面」，不会再存黑图。

3.8 终极跨组联调（真机画面 + 3组跟随 + 4组离线 AI，20 秒自动收工）：

&#x20;  `python run\_final\_real\_sim.py --self-check`       离线自检 6 项

&#x20;  `python run\_final\_real\_sim.py --props-removed`     真机联调：ArUco 识别、3组算速、4组指令、拦截流水、FPS

&#x20;  - 真帧持续 `publish\_frame` 进 SDK，3组 `MarkerTracker`/`FollowController` 用的是**同一份真画面**。

&#x20;  - 3组算出的速度走 `update\_velocity`，地面必被 `[拦截] 未起飞（地面状态）`；归零请求按设计放行。

&#x20;  - `start\_follow()` 在地面会被 3组自己的飞行状态门禁挡住（`connected\_airborne\_state\_required`），属安全设计。

&#x20;  - 4组离线 `Demo` 发「前进30厘米」：控制权交接 + 动作同样被 `[拦截]`；到点自动释放、关图传、断开。

⚠️ 同一时间只能跑一个连飞机的脚本（UDP 8889 独占）。第二个进程会报 `WinError 10048`，先关掉上一个窗口，或：

&#x20;  `Get-NetUDPEndpoint -LocalPort 8889`（查占用进程）

4\. 底层硬限制（真机模式生效）：限速 `100 cm/s`（与固件上限一致，室内 1m/s）、限高 `2.5 m`、RC 通道 ±100、地面强制 0 速度、指令超时+断连保护。详见 `API.md` 4.3。

5\. 离线回归仍全部走模拟模式，真机代码只在 `drone\_sdk/\_driver.py` 的 `connect()` 之后生效。

