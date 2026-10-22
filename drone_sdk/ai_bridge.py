"""Offline candidate for the active modules/ai entry, not the v4 archive.

Follow owns its own control grant. Visibility comes from current detections.
Normal actions use host-only handoff tickets. Land/stop report send receipts;
the wrapper retains a landing barrier until a fresh landed status is observed.
The unchanged stub provides simulated status only, never real flight evidence.
"""

from drone_sdk import controller as _ctl
import threading
import time
from contextlib import contextmanager

_landing_lock = threading.RLock()
_landing_pending = None


def _landing_in_progress():
    global _landing_pending
    with _landing_lock:
        pending = _landing_pending
        if pending is None:
            return False
        state = _ctl.get_status()
        if (state.connected is True and state.flight_state == 'landed'
                and pending['sent_at'] < state.timestamp <= time.time()
                and time.time() - state.timestamp <= .5):
            _landing_pending = None
            if state.control_owner == pending['owner']:
                _ctl.release_control(pending['owner'], control_epoch=pending['control_epoch'])
            return False
        return True


def _landing_denied():
    return dict(ok=False, status='failed', message='降落状态尚未确认，拒绝其他运动和急停',
                simulated=True)

SIMULATED = True

_REASON_TEXT = {
    "no_control": "无控制权",
    "is_landed": "尚未起飞",
    "busy": "控制权被占用",
    "failed": "执行失败",
    "timeout": "执行超时",
    "follow_no_frame_source": "没有可用画面来源，无法开始跟随",
    "follow_no_fresh_frame": "没有新鲜画面，无法开始跟随",
    "follow_calibration_failed": "跟随校准失败",
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

    def __init__(self):
        self._local = threading.local()

    @contextmanager
    def turn_guard(self, cancelled):
        previous = getattr(self._local, 'cancelled', None)
        self._local.cancelled = cancelled
        try:
            yield
        finally:
            self._local.cancelled = previous

    def _cancelled(self):
        check = getattr(self._local, 'cancelled', None)
        return bool(check and check())

    def _grant(self, command=True):
        grant = getattr(self._local, 'grant', None)
        if grant is None:
            return dict(control_epoch=-1, **({'command_epoch': -1} if command else {}))
        return dict(control_epoch=grant.control_epoch,
                    **({'command_epoch': grant.command_epoch} if command else {}))

    def _call(self, name, owner, *args):
        if self._cancelled():
            return _ctl.CommandResult(False, 'failed', 'program_cancelled')
        return getattr(_ctl, name)(owner, *args, **self._grant())

    # --- 只读 ---
    def get_status(self):
        pending = _landing_in_progress()
        s = _ctl.get_status()
        follow = _ctl._follow.status()
        return {
            "battery": s.battery,
            "height_cm": s.height_cm,
            "connected": s.connected,
            "flying": s.flight_state == "flying",
            "owner": None if s.control_owner == "none" else s.control_owner,
            "following": _ctl.is_following(),
            "target_visible": follow.get('target_visible', False),
            "flight_state": 'landing' if pending else s.flight_state,
            "landing_pending": pending,
            "timestamp": s.timestamp,
            "simulated": SIMULATED,
        }

    def get_battery(self):
        return _ctl.get_status().battery

    # --- 控制权 ---
    def request_control(self, owner):
        if self._cancelled():
            return dict(ok=False, message='本轮已取消', simulated=True)
        result = _ctl.request_control(owner)
        if result.ok:
            self._local.grant = result
        return _control_result(result)

    def release_control(self, owner):
        if _landing_in_progress():
            return _landing_denied()
        result = _control_result(_ctl.release_control(owner, **self._grant(command=False)))
        if result["ok"]:
            result["message"] = "已释放控制权"
        return result

    # --- 动作 ---
    def takeoff(self, owner):
        if _landing_in_progress():
            return _landing_denied()
        return _command_result(self._call('takeoff', owner), "起飞完成")

    def land(self, owner):
        global _landing_pending
        _ctl.stop_follow()  # Explicit land invalidates pending follow recovery.
        if _ctl.get_status().control_owner != owner:
            return dict(ok=False, status='failed', message='无控制权，未发送降落请求', simulated=True)
        with _landing_lock:
            if _landing_pending is not None:
                return dict(ok=True, status='accepted', message='降落已请求，等待状态确认',
                            landed_confirmed=False, simulated=True)
            _landing_pending = dict(sent_at=time.time(), owner=owner,
                                    control_epoch=_ctl.get_status().control_epoch)
        result = _command_result(self._call('land', owner), '降落请求已发送，等待状态确认')
        if not result['ok']:
            with _landing_lock:
                _landing_pending = None
        if result['ok']:
            result['status'] = 'accepted'
        result['landed_confirmed'] = False
        return result

    def stop_motion(self, owner):
        if _landing_in_progress():
            return _landing_denied()
        result = _command_result(self._call('stop_motion', owner), '紧急悬停请求已发送，实际停止尚未确认')
        if result['ok']:
            result['status'] = 'accepted'
        result['motion_stopped_confirmed'] = False
        return result

    def move_forward(self, owner, cm):
        if _landing_in_progress():
            return _landing_denied()
        result = self._call('move_forward', owner, cm)
        text = "前进 %d 厘米完成" % cm if result.status == 'completed' else '动作请求已发送，完成尚未确认'
        return _command_result(result, text)

    # --- 跟随 ---
    def start_follow(self):
        if _landing_in_progress():
            return _landing_denied()
        # 控制权由 3组 跟随服务内部申请，这里不能预先 request_control('follow')，
        # 否则第二次申请会返回 busy，跟随永远起不来。
        result = _ctl.start_follow()
        if not result.ok:
            message = _REASON_TEXT.get(result.reason, result.reason or "无法开始跟随")
            return {"ok": False, "message": message, "simulated": SIMULATED}
        return {"ok": True, "message": "已开始跟随", "simulated": SIMULATED}

    def stop_follow(self):
        # 3组 的 stop 内部已完成「0 速度 + 释放控制权」，这里不要再 release 一次。
        result = _ctl.stop_follow()
        if result.ok:
            message = "已停止跟随"
        else:
            message = "停止未确认：" + (result.reason or "未知原因")
        return {"ok": result.ok, "message": message, "simulated": SIMULATED}

    def is_following(self):
        return _ctl.is_following()

    def pause_follow_for_action(self):
        result, ticket = _ctl._follow.pause_for_action()
        return dict(ok=result.ok, ticket=ticket, message=result.reason, simulated=True)

    def finish_follow_action(self, ticket, status):
        if _landing_in_progress():
            return _landing_denied()
        result = _ctl._follow.finish_action(ticket, status)
        return dict(ok=result.ok, message=result.reason, simulated=True)
