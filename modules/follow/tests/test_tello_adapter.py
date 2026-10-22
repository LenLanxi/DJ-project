import time
import unittest
from types import SimpleNamespace

import numpy as np

from r3_follow.core import FollowController, MotionRequest, State
from r3_follow.tello_adapter import LatestSDKFrame, TelloCameraReader, preview_rc
from r3_follow.vision import MarkerTracker, synthetic_frame


class TelloAdapterTests(unittest.TestCase):
    def setUp(self):
        self.reader = SimpleNamespace(frame=np.zeros((480, 640, 3), np.uint8),
                                      stopped=False, with_queue=False)
        self.frames = LatestSDKFrame(self.reader)

    def fresh(self, t, visible=True):
        self.reader.frame = synthetic_frame(x=.7, visible=visible)[:, :, ::-1].copy()
        return self.frames.poll(t)

    def test_initial_cache_not_counted_and_rgb_converted(self):
        self.assertIsNone(self.frames.poll(0))
        self.reader.frame = np.full((3, 4, 3), (255, 0, 0), np.uint8)
        seq, stamp, bgr = self.frames.poll(.1)
        self.assertEqual((seq, stamp), (1, .1))
        self.assertEqual(bgr[0, 0].tolist(), [0, 0, 255])

    def test_same_frame_does_not_refresh_time(self):
        first = self.fresh(.1)
        self.assertIs(self.frames.poll(10), first)
        self.assertEqual(first[1], .1)

    def test_identical_pixels_new_decoded_frame_is_fresh(self):
        self.fresh(.1)
        self.reader.frame = self.reader.frame.copy()
        self.assertEqual(self.frames.poll(.2)[:2], (2, .2))

    def test_queue_reader_rejected(self):
        self.reader.with_queue = True
        with self.assertRaises(ValueError):
            LatestSDKFrame(self.reader)

    def test_stopped_and_bad_frames_report_errors(self):
        self.reader.frame = np.zeros((5, 5), np.uint8)
        with self.assertRaises(ValueError):
            self.frames.poll(.1)
        self.reader.stopped = True
        with self.assertRaises(RuntimeError):
            self.frames.poll(.2)

    def test_frozen_sdk_frame_triggers_controller_watchdog(self):
        tracker, controller = MarkerTracker(), FollowController(recovery_window=1.5)
        seq, stamp, bgr = self.fresh(0)
        controller.observe(tracker.detect(bgr, seq, stamp, "tello"))
        controller.calibrate(0)
        controller.start(0)
        seq, stamp, bgr = self.frames.poll(.6)
        controller.observe(tracker.detect(bgr, seq, stamp, "tello"))
        self.assertEqual(controller.tick(.6).right, 0)
        self.assertEqual(controller.state, State.STALE)

    def test_preview_scale_and_inactive_zero(self):
        result = preview_rc(MotionRequest(State.TRACKING, -.3, .2), full_scale=100)
        self.assertEqual((result.left_right, result.forward_backward), (-30, 20))
        self.assertTrue(result.preview_only)
        self.assertEqual(preview_rc(MotionRequest(State.RECOVERING, .3, .3),
                                    full_scale=100).left_right, 0)
        with self.assertRaises(ValueError):
            preview_rc(MotionRequest(State.TRACKING, float("nan")), full_scale=100)

    def test_standalone_reader_only_connects_and_streams(self):
        calls, source = [], self.reader
        source.stop = lambda: calls.append("reader.stop")
        class FakeTello:
            def connect(self): calls.append("connect")
            def streamon(self): calls.append("streamon")
            def get_frame_read(self, with_queue):
                calls.append(("get_frame_read", with_queue))
                return source
            def streamoff(self): calls.append("streamoff")
        camera = TelloCameraReader(FakeTello)
        try:
            deadline = time.monotonic() + 2
            while ("get_frame_read", False) not in calls and time.monotonic() < deadline:
                time.sleep(.01)
            time.sleep(.03)
            source.frame = synthetic_frame()[:, :, ::-1].copy()
            while camera.snapshot()[0] is None and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertIsNotNone(camera.snapshot()[0])
        finally:
            camera.done.set()
            camera.thread.join(2)
        self.assertFalse(camera.thread.is_alive())
        self.assertEqual(calls, ["connect", "streamon", ("get_frame_read", False),
                                 "reader.stop", "streamoff"])

    def test_connection_failure_is_visible(self):
        class BrokenTello:
            def connect(self): raise RuntimeError("test disconnected")
        camera = TelloCameraReader(BrokenTello)
        camera.thread.join(2)
        sample, error = camera.snapshot()
        self.assertIsNone(sample)
        self.assertIn("test disconnected", error)
