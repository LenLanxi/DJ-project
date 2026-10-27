"""DJITelloPy 2.5 video adapter and offline RC preview. No flight commands."""
from dataclasses import dataclass
import math
import threading
import time

import cv2
import numpy as np

from .core import MotionRequest, State


class LatestSDKFrame:
    """Borrow a non-queued DJITelloPy reader; never own/close team 2's device.

    SDK 2.5 replaces its RGB ndarray per decoded frame. Object identity prevents
    repeated polls from refreshing timestamps. Time is first local observation,
    NOT drone capture time or an assurance that decoder buffers are empty.
    Discard the initial cached frame; wait for a new object after attachment.
    """
    def __init__(self, reader):
        if getattr(reader, "with_queue", False):
            raise ValueError("Use get_frame_read(with_queue=False)")
        self.reader = reader
        self.previous = reader.frame
        self.latest = None
        self.seq = 0

    def poll(self, now):
        if not math.isfinite(now):
            raise ValueError("Invalid local timestamp")
        if getattr(self.reader, "stopped", False):
            raise RuntimeError("DJITelloPy video reader stopped")
        rgb = self.reader.frame
        if rgb is None or rgb is self.previous:
            return self.latest
        if (not isinstance(rgb, np.ndarray) or rgb.dtype != np.uint8
                or rgb.ndim != 3 or rgb.shape[2] != 3 or rgb.size == 0):
            raise ValueError("Expected SDK RGB uint8 frame")
        if self.latest is not None and now < self.latest[1]:
            raise ValueError("Local time moved backwards")
        bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        self.previous = rgb
        self.seq += 1
        self.latest = (self.seq, now, bgr)
        return self.latest


class TelloCameraReader:
    """Standalone, ground-only preview. Do not run beside team 2's SDK owner.

    Construction is explicit and performed only by --tello-video. connect and
    streamon do communicate with the device; this class never sends RC/takeoff.
    Blocking SDK calls run off the UI thread.
    """
    def __init__(self, tello_factory=None):
        self.lock = threading.Lock()
        self.latest = None
        self.error = "正在连接 Tello 视频"
        self.done = threading.Event()
        self.thread = threading.Thread(
            target=self._run, args=(tello_factory,), daemon=True)
        self.thread.start()

    def _run(self, factory):
        tello = reader = None
        stream_started = False
        try:
            if factory is None:
                from djitellopy import Tello
                factory = Tello
            tello = factory()
            tello.connect()
            if self.done.is_set():
                return
            tello.streamon()
            stream_started = True
            if self.done.is_set():
                return
            reader = tello.get_frame_read(with_queue=False)
            frames = LatestSDKFrame(reader)
            while not self.done.is_set():
                sample = frames.poll(time.monotonic())
                with self.lock:
                    self.latest = sample
                    self.error = "" if sample is not None else "等待 Tello 新画面"
                self.done.wait(.01)
        except Exception as exc:
            with self.lock:
                self.latest = None
                self.error = f"Tello 视频不可用：{exc}"
        finally:
            if reader is not None:
                reader.stop()
            if tello is not None and stream_started:
                try:
                    tello.streamoff()
                except Exception:
                    pass
            # No explicit end(): SDK end() can land a flying device. The local
            # object here never takes flight and is never shared with team 2.

    def snapshot(self):
        with self.lock:
            return self.latest, self.error


@dataclass(frozen=True)
class RCPreview:
    left_right: int
    forward_backward: int
    up_down: int = 0
    yaw: int = 0
    preview_only: bool = True


def preview_rc(request: MotionRequest, *, full_scale: int) -> RCPreview:
    """Illustrate SDK integer channels, not calibrated speed or permission.

    Caller must explicitly pick a provisional scale. This function cannot send.
    Real dispatch, lease/watchdog and units remain team 2's integration decision.
    """
    if type(full_scale) is not int or not 1 <= full_scale <= 100:
        raise ValueError("full_scale must be an integer in 1..100")
    if not isinstance(request, MotionRequest):
        raise ValueError("Expected MotionRequest")
    if request.state != State.TRACKING:
        return RCPreview(0, 0)
    if any(not isinstance(v, (int, float)) or not math.isfinite(v)
           or abs(v) > .3 for v in (request.right, request.forward, request.yaw, request.up)):
        raise ValueError("Invalid dimensionless motion request")
    return RCPreview(round(request.right * full_scale),
                     round(request.forward * full_scale), up_down=round(request.up * full_scale),
                     yaw=round(request.yaw * full_scale))
