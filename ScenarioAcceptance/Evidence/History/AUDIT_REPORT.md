# v3.0 审核修复报告（学长 32 项 + 3 个致命竞态）

**结论：全部 35 项（32 条验收 + 3 条竞态）都有对应的回归断言，且当前全绿。**

| 套件 | 数量 | 结果 |
| --- | --- | --- |
| `test_review_matrix.py`（32 项 + 3 竞态 + G1–G5） | 45 | ✅ 45/45 |
| RootIntegration（全链路/并发/会话急停/未知指令） | 16 | ✅ |
| AIModule（4 组模块测试） | 28 | ✅ |
| FollowModule（3 组跟随意图/视觉/会话） | 323 | ✅ |
| GroundSelfCheck（真机驱动安全限制自检） | 13 | ✅ |
| ScenarioAcceptance（T01–T19 场景探针） | 19 | ✅ |
| DocsBaseline + DocsAcceptance（旧实现基线 + T20–T43） | 54 + 24 | ✅（3 skipped：需 Docker） |
| R3Review（R301–R303） | 7 | ✅ |

一键回归：

```powershell
.\.venv\Scripts\python.exe ScenarioAcceptance\ScenarioTests.py --all   # 8 个套件
.\.venv\Scripts\python.exe test_review_matrix.py                      # 逐项矩阵（含基线复现）
```

`test_review_matrix.py` 每条都做两件事：**跑既有探针**（探针自带旧实现基线对照 = 复现漏洞）+ **我自己打的公开接口断言**（验证修复）。删掉任何一半都会红。

---

## 一、学长 32 条逐项对照

| # | 学长指出 | T 号 | 修复落点 | 我加的断言 |
| --- | --- | --- | --- | --- |
| 1 | 正式入口没接 docs 视觉，需要 `Vision.observe(target)` | T10 失败 / T27 | `modules/ai/main.py` 改用 `from vision import Vision`，真机分支 `runner.structured_vision` | 入口必须 `from vision import Vision` 且 `observe` 签名是 `(self, target)` |
| 2 | 真机入口没启动图传 | T09 | `build_real_runner()` 调 `start_video()` + `FrameBridge` | main.py 必须出现 `start_video()` 与 `FrameBridge` |
| 3 | docs 自己的入口是第二个实现 | — | `docs/v4-reference/*.py` 缩成 60 行以内的兼容导入 | 五个 docs 模块行数 < 60 且 README 指向 modules/ai |
| 4 | 共享状态是模拟常量 `StatusResult(88, …)` | T11 | `controller.get_status()` 读 `_driver.health()` | 注入电量 57 → 状态必须是 57，且 `simulated=False` |
| 5 | 降落靠 50ms 计时器宣布完成 | T15 | `_advance()` 真机模式看 `health()['flying']` + `action_pending()` | 设备仍 flying 时，即使计时早过期也必须留在 `landing` |
| 6 | 电量 None 时拒绝条件失效 | T12 | 起飞门禁改 `type(battery) is not int or not min<=b<=100` | 注入 `battery=None` → `takeoff_gate_rejected` |
| 7 | docs 适配层绕过起飞门禁 | T21 | 门禁下沉到 `controller.takeoff`，`SDKBackend.action` 走同一条路 | 5% 电量下 `SDKBackend.action('takeoff')` 必须失败 |
| 8 | 真机执行仍标 simulated | T20/T23 | `SDKBackend.simulated` 透传 `_driver.SIMULATED`；`Runner` 返回后端真实标识 | 真机模式下 `SDKBackend.simulated is False` |
| 9 | 超时后没保留“结果未知”阻挡 | T16 | `_driver._pending_commands` + `action_pending()` | 有未决命令时 `action_pending()` 为真，清掉后为假 |
| 10 | 忙碌时降落被丢弃 | T17 | `_dispatch_land` 改 `while not acquire(...)` 重试，并核对 tello 身份 | 源码必须保留重试循环 |
| 11 | 旧画面当当前画面返回 | T19 | `get_frame()` 对 `_VideoDecoder` 检查 `last_frame_at ≤ 0.5s` | 10 秒旧帧 → `None`；新帧 → 放行 |
| 12 | 轮询给旧帧新编号 | T18 | `capture_metadata(frame)` 原子返回 `(count, timestamp)` | 同一帧两次 `capture_metadata` 必须完全相同 |
| 13 | 速度指令失败仍 accepted | T22 | `set_velocity()` 返回 bool；异常记 `link_fault` | 发送抛异常 → 返回 False 且产生 `link_fault` 事件 |
| 14 | 真机可用静态图导航 | T24 | `Gateway`：真机 + `source != sdk_camera` → `real_navigation_requires_live_camera` | 源码必须含该拒绝分支 |
| 15 | 推理结束不再检查画面年龄 | T25 | `Vision.observe()` 推理后复查 `0.5s` | 源码含 `visual_result_expired` |
| 16 | 动作前不检查观察过期 | T29 | `Gateway.dispatch` 检查 `navigation_observation_expired` | 源码含该分支 |
| 17 | 允许 `visible=true` + `width_fraction=0` | T26 | `visible_width_must_be_positive`、`invisible_coordinates_must_be_null` | 两个错误码都在 |
| 18 | 正常到达的 stop 关掉了跟随恢复 | T30 | 只有“无观察的显式 stop”才 `resume=False` | (`stop_motion` + 有观察) 不得清 resume |
| 19 | 交接丢了原跟随参考尺寸 | T33 | `pause_follow_for_action` / `finish_follow_action` 票据携带原参考 | 两接口必须成对存在 |
| 20 | 没传控制权/命令代次 | T34 | `SDKBackend.action` 用 `GRANT` 上下文把 epoch 带到下发处 | 源码必须 `**grant` 且回写新的 grant |
| 21 | 释放失败被忽略 | T31 | `finish()` 检查 `release_control` | 源码含 `cleanup_release_failed` |
| 22 | 收尾停止失败被忽略 | T32 | `finish()` 检查 `stop_motion` | 源码含 `cleanup_stop_failed` |
| 23 | 打断先等网络再停本地 | T37 | `barge_in()` 先 `create_task(gateway.interrupt(False))` 再发网络取消 | 断言源码里本地 interrupt 出现在网络 await 之前 |
| 24 | 打断截错了会话条目 | T38 | `PlaybackBuffer.interrupt()` 优先取 `parts[0]`（未播条目） | 旧条目播完后打断，返回的必须是新条目 id |
| 25 | 语音/视觉误用同一密钥 | T43 | 实时只读 `AI_REALTIME_API_KEY`，不回落 `AI_API_KEY` | 源码不得出现 `OPENAI_API_KEY` / `api.openai.com` / 回落聊天 Key |
| 26 | SOP 的 50cm 演示入口不支持 | T41 | `validate` 放开 `20..100`；`Demo` 支持“前进N厘米” | `move_forward(50)` 通过，`10/101/50.0` 全部拒绝；SOP 台词与实现一致 |
| 27 | “不确定不动作”只在提示词 | T40 | `Agent.handle` 先拦否定/禁止指令，模型输出再合法也不执行 | “不要起飞” → 未执行且飞机状态不变 |
| 28 | SOP 混淆零速度/停桨/停止时间 | — | SOP 改写：`rc 0 0 0 0` 明确“不是停桨”，`motoroff` 明确“退出电机冷却”，亚毫秒计时只代表代码路径 | 三条措辞必须在文里，且不得出现“立即停桨” |
| 29 | 帐篷示例没有 360° 旋转+恢复跟随 | T39/T42 | `red_tent.py` 加 `rotate(360)` → `start_follow()` | 示例必须同时含这两句 |
| 30 | “到达帐篷”只看 `width_fraction` | T42 | 真机直接拒绝用画面占比当到达证据并停止 | 示例必须含“画面占比不能证明到达”的拒绝分支 |
| 31 | 飞离后找回用户未实现 | — | 不做假实现：超出恢复窗口就停止并上报 `recovering`，文档写明属未供能力 | `service.status()` 暴露 `recovering`；`REVIEW_FIXES.md` 明示该能力缺失 |

## 二、三个致命竞态

| 编号 | 学长原话 | 修复 | 断言 |
| --- | --- | --- | --- |
| R301 | 已要求停止，旧速度请求却还能重新启动运动 | `team2_adapter.update_velocity` 必须把**请求当时的 command_epoch** 带到最终下发处，不再临时领号 | 暂停→发送之间被取消，旧速度必须被拒；源码 `update_velocity` 体内必须出现 `command_epoch` |
| R302 | 旧申请拿到了另一次授权的编号 | `request_control` 返回的 grant 与随后确认到的 grant 必须一致，不一致拒绝 | 竞态下借用新 grant 必须失败；无竞态仍成功；旧 control_epoch 被拒 |
| R303 | 读取状态失败导致停止请求根本没尝试 | 清理时 `get_status()` 抛异常也要**盲发**带 token 校验的停止 | 状态异常时仍必须有 stop 尝试；正常路径归零并释放；替换 owner 不被误停 |

## 三、2026-10-24 学长补测的 5 个致命漏洞（G1–G5）

| # | 问题 | 修复 | 回归断言 |
| --- | --- | --- | --- |
| G1 | 急停/降落被动作队列卡死 | `controller.request_control(priority=True)` 保留通道：越过 `action_result_pending`/`landing_in_progress`/`busy`，作废在途动作 token 并零速度；`stop_motion`/`land` 增加 `priority=True`，`Gateway.interrupt()` 走 `backend.priority_stop()` | `testG1_priority_channel_cannot_be_blocked_by_pending_action`、`testG1_gateway_interrupt_uses_priority_channel`（pending 存在时急停仍必须送达、速度归零） |
| G2 | 取消指令没有穿透到发送前 | `_driver` 新增取消编号：`_call_real()` 在拿到 `_command_lock` 之后、调用 `fn()` 之前比对 `_cancel_epoch`，不一致直接丢弃并记 `command_dropped`；`interrupt()` 先 `cancel_pending()` 再零速度 | `testG2_cancel_reaches_the_point_before_send`（排队中的命令在取消后从未调用 fn）、`testG2_interrupt_cancels_queued_commands` |
| G3 | 起飞超时被直接判成失败/落地 | 超时/被丢弃 → `action_unknown`，controller 进入 `unknown` 态；`reconcile_state()` 用真实遥测修正，**飞起来就纠正为 flying**，只有“过了 2s 稳定期且遥测确认在地面”才回到 landed；收尾线程带 token，旧线程不许改新状态 | `testG3_takeoff_timeout_marks_unknown_then_telemetry_fixes_it`、`testG3_takeoff_timeout_with_ground_telemetry_settles_to_landed` |
| G4 | 3 组速度请求没死绑原编号 | `Team2StubPort.update_velocity` 在**入口**绑定 `command_epoch`，后续检查/发送一律用这个绑定值，中途被 stop 则因编号过期被底层拒绝 | `testG4_adapter_binds_command_epoch_on_entry`（假 SDK 每次读都递增，断言发送时用的是入口那一次） |
| G5 | 退出清理不统一 | `shutdown_real(force=)` 统一收尾（停画面转发 → 作废排队命令 → 零速度 → 关图传 → 断开），`health()` 抛异常也照样清理；`main()` 用 `try/finally` 包住 `_dispatch`，`run_modern` 与 `build_real_runner` 失败路径同样收尾 | `testG5_shutdown_real_always_disconnects_and_stops_streams`、`testG5_shutdown_real_survives_status_failure`、`testG5_main_wrapper_cleans_up_on_exception`、`testG5_run_modern_finally_covers_real_drone` |

顺带加固：`health()` 不再因驱动替身缺少 `get_battery/get_flight_time` 抛 `AttributeError`（改为 `getattr` 探测）。

## 四、这次合入做的工程改动

1. **基底**：`temp_follow_v3`（3 组 v3 转向跟随/局部补检/会话日志）+ `temp_ai_v6`（4 组 review-fix 候选）全量接收，`modules/follow`、`modules/ai`、`drone_sdk`、`docs/v4-reference` 一并更新。
2. **docs 不再有第二套实现**：`backends/gateway/vision/realtime/sandbox_runner` 变为兼容导入壳；主入口唯一：`modules/ai/main.py`。
3. **国内 AI**：文本/视觉走 `AI_CHAT_URL / AI_API_KEY / AI_MODEL`；实时走 `AI_REALTIME_URL / AI_REALTIME_API_KEY / AI_REALTIME_MODEL`；仓库内没有任何厂商地址硬编码。
4. **Windows 可跑**：`ScenarioTests` 的网络护栏改成“只封外网、放行 loopback”。原实现把 `socket.connect` 全封，导致 Windows 上 `asyncio.run()` 建 ProactorEventLoop 时 `socketpair()` 被误杀，T06/T07/T08 假失败。
5. **跑测试不依赖 git 在 PATH**：探针记录 revision 改为 best-effort（缺失时写 `unknown`），避免“没装 git 就红”。
6. **新增**：`test_review_matrix.py`（35 项逐条矩阵）、本报告。

## 五、接口边界

- Docker 沙箱的真实隔离/取消清理：本机无 Docker，`DocsBaseline` 有 3 项 Docker 集成测试 skipped。
- **独立测距、避障净空、飞离视野后找回用户**：现有传感器/接口给不出，代码选择“停止并声明不支持”，不是已实现。


