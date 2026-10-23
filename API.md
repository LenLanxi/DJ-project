本文件为上游原始接口文档，保留供对照。本交接候选的实际行为与新增字段请先阅读 `API_CONSOLIDATED.txt`；降落发送确认不能当作实际落地。

\# 无人机统一 SDK 接口文档 (V0.2)



\## 一、 画面与状态 (只读，无需控制权)



\### `get\_frame() -> FrameResult`

\- \*\*机制\*\*: \*\*绝不阻塞\*\*，立即返回快照。同一画面重复读取 `frame\_id` 保持不变。

\- \*\*返回字段\*\*:

&#x20; - `frame`: BGR 格式图像数组 (OpenCV)，无画面时为 `None`。

&#x20; - `frame\_id`: 帧编号。

&#x20; - `timestamp`: 采集绝对 Unix 时间戳 (用于算延迟)。

&#x20; - `stale`: 画面过期标志 (如 `time.time() - timestamp > 0.5` 即为过期)。



\### `get\_status() -> StatusResult`

\- \*\*返回字段\*\*: `battery`, `height\_cm`, `connected`, `flight\_state` ("landed"/"flying"/"unknown"), `control\_owner`, `timestamp`。



\---



\## 二、 控制权管理



\### `request\_control(owner) -> ControlResult`

\- `owner`: `"follow"` (3组) 或 `"model"` (4组)。

\- \*\*机制\*\*: 被占用时返回 `ok=False, reason="busy"`。切换瞬间会\*\*强制清除旧的速度请求\*\*。



\### `release\_control(owner) -> ControlResult`

\- \*\*机制\*\*: 释放时强制速度归零。



\---



\## 三、 连续运动控制 (平滑跟随/微调，非阻塞)



\### `update\_velocity(owner, left\_right, forward\_back, up\_down, yaw)`

\- \*\*参数单位\*\*: 范围 `-100` 到 `100`。

\- \*\*方向\*\*: `left\_right` (负左正右), `forward\_back` (负后正前), `up\_down` (负下正上), `yaw` (负逆正顺)。

\- \*\*频率与看门狗\*\*: 建议 10Hz (0.1秒一次)。\*\*独立后台线程监控，超过 0.5 秒未刷新，自动归零悬停。\*\*

\- \*\*返回\*\*: `status="accepted"`, `is\_blocking=False`。



\---



\## 四、 基础动作 (takeoff 与位移为阻塞式，land 为发送即返回)



> \*\*调用前必须持有控制权。\*\* 

> `status` 区分 `"completed"`、`"failed"`、`"timeout"`。



\### 4.1 阻塞式动作 (执行完才返回)

\- `takeoff(owner)`: 阻塞至起飞动作完成。

\- `move\_forward(owner, cm)` / `move\_back` / `move\_left` / `move\_right` / `move\_up` / `move\_down` (cm: 20\~500)

\- `rotate(owner, degrees)`

\- \*\*中断机制\*\*: 在阻塞期间，若由独立线程调用 `stop\_motion(owner)` 或 `land(owner)`，底层会强行打断当前动作。



\### 4.2 非阻塞动作 (发送即返回)

\- `land(owner)`: \*\*指令成功发送给飞控后立即返回\*\* `ok=True` / `status="completed"`。返回值\*\*不代表飞机已经落地\*\*。

\- `stop\_motion(owner)`: 同上，返回仅表示悬停指令已发送。

\- \*\*并发要求\*\*: `land` 与 `stop\_motion` 不得与阻塞动作共用锁，必须在 50ms 内返回（回归测试见 `test\_concurrency.py`）。



\### 4.3 真机模式的安全硬限制（2 组底层 `drone\_sdk/\_driver.py`）

> 默认仍是模拟模式（`SIMULATED=True`），离线测试行为不变；只有 `\_driver.connect()` 成功后才切到真机模式。

\- \*\*限速\*\*: 任何速度请求超过 `MAX\_SPEED\_CM\_S = 100` 直接拒绝（`ValueError`），与 Tello `speed` 命令固件范围 10\~100 完全一致，不再做二次夹紧。默认地面安全速度 `50 cm/s`。

\- \*\*限高 2.5m\*\*: 上升通过 `\_plan\_ascent()` 计算剩余空间，超出部分被夹紧；剩余不足 20cm 时整条上升指令被拒绝（返回 `ok=False`，事件 `height\_limit\_rejected`）。

\- \*\*下降\*\*: 被夹紧到当前高度以内，避免在地面以下继续发下降指令。

\- \*\*RC 通道\*\*: 各通道夹紧到 ±100；未起飞时强制下发 `0 0 0 0`；到达 2.5m 后向上的通道被压成 0。

\- \*\*断连保护\*\*: 所有阻塞指令都带超时与 `try/except`，连续 3 次失败把链路标记为 `link\_ok=False` 并拒绝新指令（不再抛异常给上层）；`interrupt()` 永不阻塞，先发 0 速度再把降落交给后台线程。

\- \*\*视频\*\*: `start\_video()` 走 `streamon()` + `get\_frame\_read()`，`get\_frame()` 返回真实 BGR 帧。

\- \*\*注意\*\*: `controller.get\_status()` 仍上报 `simulated=True`（离线候选层未改），真机状态以 `\_driver.health()` 为准；真机飞行尚未授权。



\---



\## 五、 跟随接口



\- `start\_follow()`: 启动跟随。

\- `stop\_follow()`: 停止跟随，释放控制权。

\- `is\_following() -> bool`: 查询状态。



\---



\## 六、 落地状态与遥测



\- \*\*判定方式\*\*: `land()` 返回后，请轮询 `get\_status().flight\_state`，变为 `"landed"` 才算真正落地。

\- \*\*数据来源\*\*: `flight\_state` 必须由飞控遥测驱动，发送降落指令时不得提前置位。

\- \*\*当前实现\*\*: `drone\_sdk` 仍是模拟层，`land()` 会把 `flight\_state` 立即置为 `"landed"`（没有遥测可依赖）。接入真机后由 `_driver` 按遥测更新，\*\*调用方代码无需改动\*\*。

\- \*\*调用方注意\*\*: 急停或降落后的自动化流程（如自动恢复跟随、自动重新起飞）必须等 `flight\_state` 变为 `"landed"` 再继续，不要在 `land()` 返回时立即认为飞机已停在地面。

