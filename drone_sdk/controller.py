# drone_sdk/controller.py
import time
import threading
from .contract import CommandResult, ControlResult, StatusResult, FrameResult

# ====== 全局变量与线程锁 ======
_current_owner = None
_flight_state = "landed"
_frame_counter = 0
_last_velocity_time = 0
_velocity_lock = threading.Lock() # 保护并发访问的锁
_following = False

# ====== 看门狗后台线程 ======
def _watchdog_thread():
    global _last_velocity_time, _current_owner
    while True:
        time.sleep(0.1) # 每0.1秒检查一次
        with _velocity_lock:
            # 如果当前有控制权，且超过0.5秒没收到新速度指令
            if _current_owner is not None and _last_velocity_time > 0:
                if (time.time() - _last_velocity_time) > 0.5:
                    print(f"\n[假飞行][看门狗] 超过0.5秒未更新速度，自动归零悬停！")
                    _last_velocity_time = 0 # 重置标记，防止一直刷屏

# 启动后台守护线程
_watchdog = threading.Thread(target=_watchdog_thread, daemon=True)
_watchdog.start()

# ====== 只读接口 ======
def get_frame() -> FrameResult:
    """获取画面快照，不阻塞。"""
    global _frame_counter
    _frame_counter += 1
    # 模拟假画面
    return FrameResult(frame=None, frame_id=_frame_counter, timestamp=time.time(), stale=False)

def get_status() -> StatusResult:
    """获取当前状态。"""
    with _velocity_lock:
        return StatusResult(
            battery=88, 
            height_cm=0 if _flight_state == "landed" else 120,
            connected=True, 
            flight_state=_flight_state,
            control_owner=_current_owner or "none",
            timestamp=time.time()
        )

# ====== 控制权接口 ======
def request_control(owner: str) -> ControlResult:
    """申请控制权。控制权交接时，强制清除旧速度。"""
    global _current_owner, _last_velocity_time
    with _velocity_lock:
        if _current_owner is not None and _current_owner != owner:
            return ControlResult(ok=False, current_owner=_current_owner, reason="busy")
        
        if _current_owner != owner:
            print(f"\n[假飞行][控制权] 切换至 {owner}，旧速度请求已强制清零")
            _last_velocity_time = 0
            
        _current_owner = owner
        return ControlResult(ok=True, current_owner=owner)

def release_control(owner: str) -> ControlResult:
    """释放控制权。释放时强制清除旧速度。"""
    global _current_owner, _last_velocity_time
    with _velocity_lock:
        if _current_owner == owner:
            print(f"\n[假飞行][控制权] {owner} 释放控制权，速度强制归零悬停")
            _last_velocity_time = 0
            _current_owner = None
        return ControlResult(ok=True, current_owner="none")

# ====== 连续速度控制 (3组重点) ======
def update_velocity(owner: str, left_right: int, forward_back: int, up_down: int, yaw: int) -> CommandResult:
    """连续控制，新指令覆盖旧指令，带看门狗。"""
    global _last_velocity_time
    with _velocity_lock:
        if _current_owner != owner: 
            return CommandResult(ok=False, reason="no_control")
        
        _last_velocity_time = time.time() # 刷新看门狗时间
        # 模拟打印，防止刷屏，这里简单过滤一下全0的情况
        if left_right or forward_back or up_down or yaw:
            print(f"[假飞行][速度] {owner} 速度更新: 左右{left_right}, 前后{forward_back}, 上下{up_down}, 偏航{yaw}")
        return CommandResult(ok=True, status="accepted", is_blocking=False)

# ====== 基础动作 (阻塞式) ======
def _mock_move(owner: str, action_name: str, param: int) -> CommandResult:
    if _current_owner != owner:
        return CommandResult(ok=False, status="failed", reason="no_control")
    if _flight_state == "landed":
        return CommandResult(ok=False, status="failed", reason="is_landed")
    
    print(f"[假飞行][动作] {owner} 执行 {action_name}: {param}")
    # 真实 djitellopy 这里是阻塞的，所以 is_blocking=True
    return CommandResult(ok=True, status="completed", reason="", is_blocking=True)

def takeoff(owner: str) -> CommandResult:
    global _flight_state
    if _current_owner != owner: return CommandResult(ok=False, reason="no_control")
    _flight_state = "flying"
    print(f"[假飞行][动作] {owner} 执行 起飞")
    return CommandResult(ok=True, status="completed")

def land(owner: str) -> CommandResult:
    global _flight_state
    if _current_owner != owner: return CommandResult(ok=False, reason="no_control")
    _flight_state = "landed"
    print(f"[假飞行][动作] {owner} 执行 降落")
    return CommandResult(ok=True, status="completed")

def stop_motion(owner: str) -> CommandResult:
    """最高优先级：紧急悬停。"""
    if _current_owner != owner: return CommandResult(ok=False, reason="no_control")
    print(f"[假飞行][紧急] {owner} 执行 紧急悬停")
    return CommandResult(ok=True, status="completed")

def move_forward(owner: str, cm: int) -> CommandResult: return _mock_move(owner, "前进", cm)
def move_back(owner: str, cm: int) -> CommandResult: return _mock_move(owner, "后退", cm)
def move_left(owner: str, cm: int) -> CommandResult: return _mock_move(owner, "左移", cm)
def move_right(owner: str, cm: int) -> CommandResult: return _mock_move(owner, "右移", cm)
def move_up(owner: str, cm: int) -> CommandResult: return _mock_move(owner, "上升", cm)
def move_down(owner: str, cm: int) -> CommandResult: return _mock_move(owner, "下降", cm)
def rotate(owner: str, degrees: int) -> CommandResult: return _mock_move(owner, "旋转", degrees)

# ====== 跟随专用接口 ======
def start_follow() -> ControlResult:
    global _following
    _following = True
    print("\n[假飞行][跟随] 开始跟随模式")
    return ControlResult(ok=True, current_owner="follow")

def stop_follow() -> ControlResult:
    global _following
    _following = False
    print("\n[假飞行][跟随] 停止跟随模式，已悬停")
    return ControlResult(ok=True, current_owner="none")

def is_following() -> bool:
    return _following