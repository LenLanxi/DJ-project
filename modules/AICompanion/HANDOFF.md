# 4 组与公共 SDK 对接记录

基线：LenLanxi/DJ-project，main 提交 aa40228（2026-10-21）。
本组仅新增 modules/AICompanion，不修改公共 SDK、根文档和根依赖。

## 已核对并集成

- 环境按上游使用 Python 3.11；共享依赖固定为 djitellopy 2.5.0、opencv-python 4.8.0.76、numpy 1.26.4。
- SdkRunner 仅从 drone_sdk.controller 导入；不创建 Tello，不访问私有驱动。
- StatusResult 使用 battery/height_cm/connected/flight_state/control_owner/timestamp。
- FrameResult 浅层转换为字典，不深拷贝 BGR 数组，不捏造新帧时间和编号。
- request_control("model") 成功后才调用动作；busy 时不执行、不释放别人的控制权。
- 动作使用 takeoff(owner)、land(owner)、stop_motion(owner)、move_forward(owner, cm)。
- SDK 结果保留 ok/status/reason/is_blocking；accepted 不等于 completed。
- 动作失败、超时或抛异常仍尝试释放 model 控制权；不自动重试动作。
- 语法 drone.get_battery() 由本组翻译为 get_status().battery。
- 完全离线入口保留 MockDrone；SDK 同步入口使用同一白名单校验和 Agent 规划逻辑。

## 上游当前实现与文档的差异（本 PR 不修改公共代码）

- _driver.py 尚未实现；状态固定，动作只打印假飞行。
- get_frame() 无画面仍递增编号、更新时间并返回 stale=False，不符合文档快照语义。
- start_follow()/stop_follow() 只切换布尔值，不实际申请/释放控制权。
- 阻塞动作、独立线程中断、实际速度归零均尚无真实驱动验证。
- release_control() 在调用者非持有者时也返回成功；适配器只释放自己成功申请的 model 控制权。

## 尚未开放的能力

- SDK 跟随启停返回 follow_not_integrated，不沿用本地 Mock 的自动恢复承诺。
- SDK 正在跟随或 owner=follow 时，不尝试接管，包括停止/降落；等待 2、3 组完成优先中断协议。
- 尚无 update_velocity 循环；语音动作当前仅允许一次前进30厘米。
- Session 只用于即时 Mock；SDK 通过同步 SdkDemo 调用，拒绝挂入现有异步 Session。
- SDK 画面已可转换成字典，但未接到终端视觉入口。
- 麦克风、耳机、付费聊天/语音/视觉服务和真机均未验证。

待公共 SDK 完成跟随交接、停止确认和阻塞中断后，再联合 3 组验证目标丢失、帧过期、恢复条件与整机行为。
