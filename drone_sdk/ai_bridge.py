"""2组统一 SDK 与 4组 AI 框架之间的适配层。

4组 modules/ai/drone_adapter.py 中的 MockDrone 采用 dict 风格返回值
（{'ok': bool, 'message': str}）以及 flying / owner / following 字段；
本模块把这些调用翻译成 drone_sdk.controller 的 dataclass 接口，
4组的 31 个文件一行都不用改。

已知语义差异（对照 modules/ai/HANDOFF.md）：
- 我们没有 get_battery()，由 get_status().battery 推导；
- 我们用 flight_state / control_owner，空闲值是字符串 "none"，4组用 None；
- 我们没有 target_visible（属于3组跟踪状态），暂时恒为 True；
- controller.start_follow() 本身不做控制权仲裁，这里按 HANDOFF 约定
  先 request_control('follow') 再启动，保证与 3组/4组 的 owner 语义一致。
"""

from drone_sdk import controller as _ctl

SIMULATED = True

_REASON_TEXT = {
    "no_control": "无控制权",
    "is_landed": "尚未起飞",
    "busy": "控制权被占用",
    "failed": "执行失败",
    "timeout": "执行超时",
}


def _command_result(result, ok_message):
    """CommandResult -> 4组期望的 dict。"""
    if result.ok:
        message = ok_message
    else:
        message = _REASON_TEXT.get(result.reason, result.reason or result.status or "执行失败")
    return {"ok": result.ok, "status": result.status, "message": message, "simulated": SIMULATED}


def _control_result(result):
    """ControlResult -> 4组期望的 dict。"""
    if result.ok:
        return {"ok": True, "message": "已获得控制权", "simulated": SIMULATED}
    owner = result.current_owner or "未知"
    return {"ok": False, "message": "控制权被占用：%s" % owner, "simulated": SIMULATED}


class UnifiedDrone:
    """把 drone_sdk.controller 包装成 4组 MockDrone 的调用形状。"""

    # --- 只读 ---
    def get_status(self):
        s = _ctl.get_status()
        return {
            "battery": s.battery,
            "height_cm": s.height_cm,
            "connected": s.connected,
            "flying": s.flight_state == "flying",
            "owner": None if s.control_owner == "none" else s.control_owner,
            "following": _ctl.is_following(),
            "target_visible": True,
            "timestamp": s.timestamp,
            "simulated": SIMULATED,
        }

    def get_battery(self):
        return _ctl.get_status().battery

    # --- 控制权 ---
    def request_control(self, owner):
        return _control_result(_ctl.request_control(owner))

    def release_control(self, owner):
        result = _control_result(_ctl.release_control(owner))
        if result["ok"]:
            result["message"] = "已释放控制权"
        return result

    # --- 动作 ---
    def takeoff(self, owner):
        return _command_result(_ctl.takeoff(owner), "起飞完成")

    def land(self, owner):
        return _command_result(_ctl.land(owner), "降落完成")

    def stop_motion(self, owner):
        return _command_result(_ctl.stop_motion(owner), "已紧急悬停")

    def move_forward(self, owner, cm):
        return _command_result(_ctl.move_forward(owner, cm), "前进 %d 厘米完成" % cm)

    # --- 跟随 ---
    def start_follow(self):
        claimed = _ctl.request_control("follow")
        if not claimed.ok:
            return _control_result(claimed)
        _ctl.start_follow()
        return {"ok": True, "message": "已开始跟随", "simulated": SIMULATED}

    def stop_follow(self):
        result = _ctl.stop_follow()
        _ctl.release_control("follow")
        message = "已停止跟随" if result.ok else "停止跟随失败"
        return {"ok": result.ok, "message": message, "simulated": SIMULATED}

    def is_following(self):
        return _ctl.is_following()
