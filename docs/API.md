# 结构化动作与内部接口

模型提示和工具契约集中在 app/Tools.py。云模型使用 execute_action 和 interrupt 两个工具。宿主只接受白名单动作与参数，不执行模型生成的代码，不需要 Docker。

## 工具请求

```json
{"action":"move_forward","args":[20]}
```

每次 execute_action 只提交一个动作。多步骤目标按“前摄像头观察 → 动作 → 结果和新画面 → 下一步”循环；模型应持续处理已授权的目标，而不是每一步等待用户重说。停止调用 interrupt(land=false)，降落调用 interrupt(land=true)，优先于普通动作执行。

| action | args 与含义 |
| --- | --- |
| get_status | []；电量、高度、连接、flight_state、控制权及 continuous_motion。 |
| is_following | []；返回是否正在跟随，恢复等待不等于跟随中。 |
| get_frame | []；真实图像与时间、来源；图像载荷不作为云工具文本传输。 |
| takeoff / land / stop_motion | []；起飞需显式授权与预检，停止不证明已物理刹停。 |
| move_forward / move_back / move_left / move_right / move_up / move_down | [cm]，整数 20..500，不接受 bool；自然目标接近优先 20 cm 短步后重新观察。 |
| rotate | [degrees]，非零整数 -360..360，正顺时针、负逆时针。 |
| update_velocity | [lr,fb,ud,yaw]，四整数 -100..100；单发 RC，0.3 秒未续发看门狗归零。 |
| start_velocity | 同上；宿主每 100 ms 续发，正常动作返回后仍运行，直到停止、新运动、跟随切换、失败或会话退出。仅用于明确要求持续运动的任务。全零停止。 |
| start_follow / stop_follow | []；开启或显式停止标记跟随。 |

RC 请求值不是 cm/s。continuous_motion 的 running 只证明宿主在续发，不证明物理速度或位移，last_reason 保留停止原因。

continuous_motion.channels 是持续任务的请求值，last_command 是最近一次 SDK 结果，包含裁剪后 applied_channels 与 wire_sent；停止后清空。持续运动中裁剪状态变化会记录 continuous_motion_clamp_changed。

仅用户明确调整跟随距离或构图时，平移动作可附加 adjust_follow_reference。depth 对应前后，horizontal 对应左右，vertical 对应上下。例如：

```json
{"action":"move_back","args":[20],"adjust_follow_reference":"depth"}
```

这仍只有一次物理平移。宿主在同一次控制交接内完成平移并登记参考调整，只有动作成功、后续新帧有效且跟随恢复成功才应用。普通临时任务不附加此字段，保留原跟随参考。

## 结果与不确定性

工具结果保留 ok、status、reason、journal、action_result；动作失败保留 action_error/details，包括原始底层原因、阶段、wire_sent 与 outcome_unknown。SDK 回复超时表示结果未知，不等于没有执行，不自动重发。

成功结果也保留 details。平移的 requested/applied、RC 的 requested_channels/applied_channels 区分请求参数与裁剪后的参数；clamped 和 clamp_reason 说明高度保护等限制。completed 只针对实际发送的命令，不代表原请求完整执行。wire_sent=false 表示没有实际发送网络指令；simulated、motion_verified 区分模拟与真实运动证据，参数本身不是实测位移。

跟随暂停、控制权取得和释放仍由 Host 管理。成功临时动作尝试按票据恢复原跟随；普通恢复失败单独报告 follow_cleanup，不把已完成位移改判失败。取消、停止或失败不自动恢复。控制清理未确认时只允许状态查询、停止和降落。

运动结果回填前最多等待 2 秒获取动作后的新帧。camera_observation.status=published 只表示提交到本地 RTC 视频轨，cloud_verified=false；不能证明云端选择了哪张图片。unavailable 不篡改动作结果，模型须等待实际新画面再做视觉决策。

command_channel_ambiguous 是诊断提示，不阻止后续运动；后续 ok 可能属于前一指令，结合遥测与新画面判断，不盲目重复结果未知的动作。动作回复等待使用整个动作截止时间，平移预算根据距离与配置速度计算，不受底层固定 5 秒截断。

## 维护契约

UnifiedRuntime(device, follow, vision=None, log=None, *, runner=None, methods=None, timeout=90) 拥有动作准入、取消及清理。入口 await runtime.run(request, expected_generation=...)。ActionRunner 只调用 Host.dispatch，不执行字符串，不启动模型代码子进程。

Host 持有单次 ControlGrant、取消事件、跟随票据及参考调整。SDK 在实际发送前检查授权，模型不能提交 grant。begin_interrupt 同步撤权，finish_interrupt 异步停止；降落意图不能被普通停止降级。迟到旧请求不能借用新授权。

validate_action 是外部动作参数入口，validate_tool 校验工具名及参数。SDK controller 负责公开设备接口，Host 负责交接与收尾。内部组件使用固定结果契约。

跟随算法固定 adaptive / relative_size / responsive_v1。短时画面无效时归零等待目标恢复；显式停止不自动重启。设备 get_frame 返回原始 FrameResult 身份与时间，重复读取不能刷新图像时效。

豆包凭据仅由 providers/DoubaoConfig.py 加载。网页只取得临时 RTC token；长期密钥不进入网页、工具参数、日志或发行包。
