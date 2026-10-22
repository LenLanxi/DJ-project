"""3组（跟随）服务的接入层。

把 modules/follow 的 FollowService 绑定到统一 SDK：

- 控制权由 3组 服务内部申请 / 释放（外层不要再预先 request_control('follow')，
  否则第二次申请会返回 busy）；
- 画面来源由本层统一注入：真机由 _driver 提供，测试用合成画面；
- start/stop 必须保持非阻塞语义：stop 内部会发 0 速度并释放控制权。

本模块不 import djitellopy，也不直接接触硬件。
"""

import sys
import threading
import time
from pathlib import Path

from .contract import ControlResult

REPOSITORY = Path(__file__).resolve().parent.parent
MODULES_DIR = REPOSITORY / "modules" / "follow"

FIRST_FRAME_TIMEOUT = 1.0
RECOVERY_WINDOW = 3


class FollowIntegration:
    """3组跟随服务的生命周期管理。"""

    def __init__(self, owner_getter=None):
        self._lock = threading.RLock()
        self._owner_getter = owner_getter or (lambda: "none")
        self._bridge = None
        self._port = None
        self._frame_provider = None
        self._last_events = []

    # ---------- 对外接口 ----------
    def set_frame_provider(self, provider):
        """注入画面来源；provider 需返回 FrameResult(frame/frame_id/timestamp/stale)。"""
        with self._lock:
            self._frame_provider = provider
            if self._port is not None:
                self._port.frame_provider = provider

    def start(self, timeout=FIRST_FRAME_TIMEOUT):
        with self._lock:
            if self._bridge is not None and self._bridge.is_following():
                return ControlResult(ok=True, current_owner="follow")

            try:
                bridge = self._ensure_bridge()
            except Exception as exc:  # 3组包缺失、哈希未复核、SDK 被改动等
                return self._fail("follow_unavailable: %s" % exc)

            if self._frame_provider is None:
                probe = bridge.service.port.get_frame()
                if probe.frame is None:
                    return self._fail("follow_no_frame_source")

            if not self._wait_for_fresh_frame(bridge, timeout):
                return self._fail("follow_no_fresh_frame")

            if bridge.service.controller.reference_size is None:
                calibrated = bridge.service.calibrate_reference()
                if not calibrated.ok:
                    return self._fail(calibrated.reason or "follow_calibration_failed")

            return bridge.start_follow()

    def stop(self):
        with self._lock:
            bridge, port = self._bridge, self._port
            self._bridge = self._port = None
            if bridge is None:
                return ControlResult(ok=True, current_owner="none")
            try:
                result = bridge.stop_follow()
            finally:
                # 停止过程中的 0 速度 / 释放事件要留下来供诊断，再关闭线程。
                self._last_events = list(port.events) if port is not None else []
                bridge.close()
            return result

    def is_following(self):
        with self._lock:
            return bool(self._bridge and self._bridge.is_following())

    def status(self):
        with self._lock:
            if self._bridge is None:
                return {"state": "stopped", "following": False, "background_running": False}
            return self._bridge.get_follow_status()

    def events(self):
        """诊断用：3组适配层记录的事件序列（velocity_call / stop_acknowledged 等）。

        停止后会保留最后一次会话的事件快照，便于验证"停止时发出了 0 速度"。
        """
        with self._lock:
            if self._port is not None:
                return list(self._port.events)
            return list(self._last_events)

    # ---------- 内部 ----------
    def _fail(self, reason):
        return ControlResult(ok=False, current_owner=self._owner_getter(), reason=reason)

    def _ensure_bridge(self):
        if self._bridge is not None:
            return self._bridge
        if str(MODULES_DIR) not in sys.path:
            sys.path.insert(0, str(MODULES_DIR))
        from r3_follow.team2_adapter import Team2StubPort
        from r3_follow.team2_bridge import Team3FollowBridge

        port = Team2StubPort(REPOSITORY, frame_provider=self._frame_provider)
        bridge = Team3FollowBridge(port, recovery_window=RECOVERY_WINDOW)
        bridge.service.start_background()
        self._last_events = []
        self._port, self._bridge = port, bridge
        return bridge

    @staticmethod
    def _wait_for_fresh_frame(bridge, timeout):
        service = bridge.service
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            observation = service.controller.observation
            if observation is not None:
                age = service.clock() - observation.acquired_at
                if 0 <= age <= service.controller.max_age:
                    return True
            time.sleep(0.01)
        return False
