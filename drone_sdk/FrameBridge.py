"""Publish coherent decoder identities from the real decoder to SDK consumers.

There is no camera opening or hardware connection on import. A decoder without
decoder metadata is rejected rather than assigning a fresh timestamp to old data.
"""
import threading
import time

from . import _driver as driver, controller
from .contract import FrameResult


class FrameBridge(threading.Thread):
    def __init__(self, fps=30, logger=None):
        super().__init__(daemon=True, name='frame-bridge')
        if not 0 < float(fps) <= 120:
            raise ValueError('Frame publication rate must be in (0, 120]')
        self.interval = 1 / float(fps)
        self._halt = threading.Event()
        self.published = 0
        self.last_frame = None
        self.last_error = None
        self.log = logger or (lambda event: None)

    def publish_once(self):
        frame = driver.get_frame()
        if frame is None:
            return False
        metadata = driver.capture_metadata(frame)
        if metadata is False or metadata is None:
            self.last_error = 'capture_metadata_unavailable'
            return False
        frame_id, decoded_at = metadata
        if frame_id < self.published:
            self.last_error = 'capture_identity_regressed'
            return False
        age = time.time() - decoded_at
        controller.publish_frame(FrameResult(frame, frame_id, decoded_at,
                                              not 0 <= age <= .5))
        self.published, self.last_frame = frame_id, frame
        self.last_error = None
        return True

    def run(self):
        while not self._halt.wait(self.interval):
            try:
                self.publish_once()
            except Exception as exc:
                self.last_error = type(exc).__name__
                self.log({'event': 'frame_publish_failed', 'error': self.last_error})

    def stop(self):
        self._halt.set()
