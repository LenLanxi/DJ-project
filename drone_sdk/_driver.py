"""底层通信层。

当前为模拟实现：execute() 只按模拟时长睡眠，让上层在开发阶段就能观察到
"阻塞动作"的真实时序；接入真机时在这里替换为 djitellopy 调用
（Tello.forward / Tello.takeoff 等）。

注意：land / stop_motion 属于最高优先级中断通道，不经过本层阻塞。
"""

import threading
from time import sleep

_simulated_delay = 0.0
_delay_lock = threading.Lock()  # 只保护延时变量本身，绝不包住动作执行


def set_simulated_delay(seconds: float) -> None:
    """设置阻塞动作的模拟时长（秒）。仅用于测试与本地联调。"""
    global _simulated_delay
    with _delay_lock:
        _simulated_delay = max(0.0, float(seconds))


def simulated_delay() -> float:
    with _delay_lock:
        return _simulated_delay


def execute(action: str, param=None) -> bool:
    """执行一个阻塞动作，返回是否成功。"""
    delay = simulated_delay()
    if delay:
        sleep(delay)
    return True
