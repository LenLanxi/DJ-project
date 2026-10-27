# 模型程序与 SDK 接口

正式模型说明同时写入 `modules/ai/main.py` 提示词。下列模型接口由隔离容器中的 `drone` 提供，RPC 只允许位置参数。

| 接口 | 参数、结果和边界 |
| --- | --- |
| `get_status()` | 只读字典；电量、厘米高度、连接、飞行状态、owner、授权编号、模拟标识。未知值不能当成正常。 |
| `is_following()` | 正在跟随才为 true；恢复等待不等于正在跟随。 |
| `get_frame()` | 真实图像与原解码编号/时间；没有图像就失败。时间戳是电脑解码完成时间，不证明相机曝光年龄。 |
| `describe(question)` | 视觉问答；返回 answer、解码时间与解码后图像年龄。只读，不授权运动。 |
| `observe(target)` | 结构化可见性、水平中心、宽度占比及解码身份。无法判断就不可见；不能推出米数、通路或到达。 |
| `takeoff()` | 起飞须显式运行授权，且通过真实连接、新鲜遥测、电量、温度检查。 |
| `land()` | 请求后等待新鲜落地确认，有期限；未确认就失败。 |
| `stop_motion()` | 停止并取消后续排队动作，不自动恢复跟随；不能据此声称离散动作已物理刹停。 |
| `move_forward/back/left/right/up/down(cm)` | 各为单独方法，如 move_forward；整数 20..500 厘米。true/false 不算整数。高度与遥测门限仍适用。 |
| `rotate(degrees)` | 非零整数 -360..360；正顺时针、负逆时针，实际方向待现场核对。 |
| `update_velocity(lr,fb,ud,yaw)` | 四整数 -100..100，属于 RC 请求值，不是厘米/秒；看门狗会归零。 |
| `start_follow()` / `stop_follow()` | 无参数；调用本地跟随服务。控制交接失败不伪报成功。 |
| `adjust_follow_reference(axis)` | axis为depth/horizontal/vertical；仅已有跟随被本任务暂停、且对应轴平移已完成时接受。随后不再运动。动作后新帧有效且程序成功时才更新该轴参考并恢复跟随；accepted不是应用完成。默认临时动作不更新参考。 |

程序可使用分支、循环和 Python 标准库，但只在无网络、只读、无宿主挂载的受限 Docker 中运行。程序有时间、请求次数和输出预算，不允许无限执行。`print` 进入有上限的调试输出。

临时动作的流程：暂停已有跟随并保留恢复凭据 → 取得 model 授权 → 执行动作 → 释放授权 → 成功且目标仍可用时恢复原跟随。显式停止、降落、中断和动作失败不自动恢复。不能通过重新启动跟随偷偷重新标定距离。

SDK 内部接口另外接受 owner 与 `control_epoch` / `command_epoch`。授权编号由可信网关持有，模型不能请求控制权或提供任意授权；同名 owner 不代表同一次授权。`CommandResult.accepted` 与 `completed` 必须区分。

RTC 模型只有 `run_program(code)` 和 `interrupt(land: boolean)` 两个工具，不直接访问无人机、文件或密钥。工具结果必须与对应 ToolCallID 一一回传。
