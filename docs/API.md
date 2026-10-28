# 模型程序与内部接口

模型提示和工具契约集中在 `app/Tools.py`；动作宿主在 `execution/Host.py`。云模型只有 `run_program(code)` 和 `interrupt(land: boolean)` 两个工具，不直接访问设备、文件或密钥。工具结果须与原工具 ID 对应；豆包由服务端 `UpdateVoiceChat` 回传。

## 完整 Python

`code` 是完整 Python，支持变量、条件、循环、函数及标准库。沙箱预置全局 `drone`，不要 `import drone`，也不存在裸 `get_status()` 函数。例如：

```python
state = drone.get_status()
if not state["connected"]:
    raise RuntimeError("device_not_connected")
print(state)
```

程序仅在无网络、只读、无宿主挂载的受限 Docker 中执行，无宿主回退；有时间、请求次数和输出预算。`print` 是受限调试输出，不是新增设备接口。两家本地程序上限均为 60000 UTF-8 字节；豆包真实 RTS 包仍有独立约束，不能据此认定底层通道支持 60 KB。

## drone 接口

RPC 只允许位置参数；返回字典中的 `ok`、`status`、`reason` 必须检查。失败应抛错停止，不重复可能已执行的动作。

| 接口 | 参数、结果与边界 |
| --- | --- |
| `get_status()` | 电量、厘米高度、连接、`flight_state`、`control_owner`、授权编号及模拟标识。飞行状态为 landed/flying/landing/unknown，未知不能当正常。 |
| `is_following()` | 正在跟随才为 true；恢复等待不等于正在跟随。 |
| `get_frame()` | 带来源、原解码编号/时间的图像；没有有效画面就失败。解码时间不证明曝光时刻。 |
| `describe(question)` | 只读画面问答，返回 answer 与图像年龄；不能授权运动。 |
| `observe(target)` | 结构化目标观察，含 visible、ambiguous、candidate_count、bbox、bbox_format、center_x、width_fraction、description、frame_id、timestamp。bbox 是归一化 `[左,上,右,下]`，格式 xyxy；不是距离、通路或到达证明。 |
| `takeoff()` | 需显式起飞授权，通过真实连接、新鲜遥测、电量和温度检查；不可重复起飞。 |
| `land()` | 请求后等待新鲜落地确认，有期限；未确认就失败。 |
| `stop_motion()` | 停止并取消排队动作，取消自动恢复跟随；不证明已物理刹停。 |
| `move_forward/back/left/right/up/down(cm)` | 各为单独方法，整数 20..500 cm；bool 不算整数。导航观察后的平移限定每次 20 cm，并消耗该观察，下一步须重新 observe。 |
| `rotate(degrees)` | 非零整数 -360..360，正顺时针、负逆时针；现场需核对实际方向。 |
| `update_velocity(lr, fb, ud, yaw)` | 四整数 -100..100，是 RC 请求值而非 cm/s；看门狗会归零。 |
| `start_follow()` / `stop_follow()` | 无参数。已有跟随被本程序暂停时，start_follow 只登记成功后恢复，以最终 follow_cleanup 为准；stop_follow 是显式停止。 |
| `adjust_follow_reference(axis)` | axis 为 depth/horizontal/vertical；须已有暂停票据且对应轴平移已完成，此后不再运动。动作后新帧有效且整段程序成功才应用；accepted 尚未生效。 |

`observe` 会临时接管、归零并等待图像稳定，再用云语义种子与本地有界跟踪关联到新鲜帧。源帧、语义历史窗口与最终动作各有时效检查，详见 [Architecture](Architecture.md)。丢失、歧义或跟踪不可靠会失败。自然目标导航须有限步数/总时限，不能用图像占比声称实际到达。

临时动作默认保留原跟随距离与构图；成功且原目标仍有效才恢复。停止、降落、中断或失败不自动恢复。红帐篷任务不应重设跟随参考。程序被接收或正常退出不证明整段任务完成；最终还需检查输出、动作状态和 `follow_cleanup`。

## 内部维护契约

- `UnifiedRuntime(device, follow, vision=None, log=None, *, runner=None)` 显式注入依赖。程序入口为 `await runtime.run(code, expected_generation=...)`；诊断也走这一路，不直接调用 runner 跳过准入/收尾。
- Runtime 为每段程序创建 `Host(device, follow, vision, ...)`。Host 持有本次授权、取消事件、跟随票据和导航证据，取消后不复用；SDK 在实际发送前再次检查授权。模型不能提供任意授权编号。
- `begin_interrupt()` 同步撤权并返回停止凭据，`finish_interrupt()` 执行异步停止；后续外部停止会使旧凭据失效。降落意图不能被普通停止降级。
- 待清理 Host 未确认时先重试；仍失败则只允许状态/跟随状态查询、停止和降落。不承诺恢复模式可读取画面，不允许新运动或导航。
- `FollowService(device, config=...)` 拥有常驻识别；`stop()` 停运动并作废恢复票据，`close()` 确认线程退出；临时交接用 `pause_if_active()` / `finish_action()`。
- 设备内部 `get_frame()` 唯一返回 SDK `FrameResult(frame, frame_id, timestamp, stale)`；不得因重复读取或相同像素而编造新身份，也不能把重复帧刷新成新帧。

内部接口按已解析对象工作；边界参数验证、取消检查和最终发送检查不能为迁移测试而删去。结果中的 accepted 与 completed、软件停止与物理停止必须区分。
