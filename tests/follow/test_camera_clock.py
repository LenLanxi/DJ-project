"""Local camera clock regressions; no camera, GUI, network, or log files."""
import math
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import cv2
import numpy as np
from tests.helpers.CameraFixture import make_service
from follow.r3_follow.core import Observation
from follow.r3_follow.vision import synthetic_frame


class CoarseClock:
    """Model independent wall time and the Windows 15.625 ms tick clock."""
    def __init__(self, phase=.014):
        self.now = 100.0 + phase

    def mono(self):
        return math.floor(self.now / .015625) * .015625

    def wall(self):
        return 1800000000.0 + self.now


class Reader:
    latest = None

    def snapshot(self):
        return self.latest, ""


class Detector:
    def detect(self, frame, frame_id, acquired_at, source):
        return Observation(frame_id, acquired_at, source, 640, 480, 0,
                           True, 320, 240, .25,
                           corners=((240, 160), (400, 160),
                                    (400, 320), (240, 320)))


class CameraClockTests(unittest.TestCase):
    def setUp(self):
        for target in ("cv2.VideoCapture", "socket.socket"):
            guard = patch(target, side_effect=AssertionError(target + " forbidden"))
            guard.start()
            self.addCleanup(guard.stop)

    def create(self, phase=.014):
        clock, reader = CoarseClock(phase), Reader()
        port, service = make_service(reader, monotonic=clock.mono,
                                     wall=clock.wall, log_directory=False)
        service.tracker = Detector()
        self.addCleanup(service.close)
        return clock, reader, port, service

    def publish(self, clock, reader, frame_id, now, acquired=None):
        clock.now = now
        reader.latest = (frame_id, clock.mono() if acquired is None else acquired,
                         object())

    def start(self, clock, reader, service):
        self.publish(clock, reader, 1, 100.030)
        self.assertTrue(service.capture_once())
        self.assertTrue(service.calibrate_reference().ok)
        self.assertTrue(service._start_tracking().ok)

    def test_fresh_frame_across_coarse_tick_keeps_tracking(self):
        clock, reader, port, service = self.create()
        self.start(clock, reader, service)
        self.publish(clock, reader, 2, 100.032)
        packet = port.get_frame()
        # This is the original failure: independent wall time sees a future
        # timestamp even though the reader just completed this frame.
        self.assertLess(clock.wall() - packet.timestamp, -.010)
        self.assertEqual(service.wall() - packet.timestamp, 0.0)
        self.assertTrue(service.capture_once())
        self.assertTrue(service.is_following())
        self.assertEqual(service.status()["state"], "tracking")

    def test_cached_frame_does_not_refresh_timestamp_or_acquisition(self):
        clock, reader, port, service = self.create()
        self.start(clock, reader, service)
        packet = port.get_frame()
        acquired_at = service.controller.observation.acquired_at
        clock.now = 100.200
        self.assertEqual(port.get_frame().timestamp, packet.timestamp)
        self.assertFalse(service.capture_once())
        self.assertEqual(service.controller.observation.acquired_at, acquired_at)
        clock.now = 100.700
        expired = port.get_frame()
        self.assertEqual(expired.timestamp, packet.timestamp)
        self.assertTrue(expired.stale)
        self.assertIsNone(expired.frame)
        self.assertFalse(service.capture_once())
        self.assertFalse(service.is_following())

    def test_true_future_frame_is_rejected(self):
        clock, reader, port, service = self.create()
        self.start(clock, reader, service)
        clock.now = 100.064
        self.publish(clock, reader, 2, clock.now, clock.mono() + .1)
        self.assertLess(service.wall() - port.get_frame().timestamp, 0)
        self.assertFalse(service.capture_once())
        self.assertEqual(service.last_reason, "frame_unavailable_or_expired")
        self.assertFalse(service.is_following())

    def test_true_expired_new_frame_is_rejected(self):
        clock, reader, port, service = self.create()
        self.start(clock, reader, service)
        clock.now = 100.800
        acquired = clock.mono() - .6
        self.publish(clock, reader, 2, clock.now, acquired)
        packet = port.get_frame()
        self.assertEqual(packet.timestamp, port.epoch_offset + acquired)
        self.assertTrue(packet.stale)
        self.assertGreater(service.wall() - packet.timestamp, .5)
        self.assertFalse(service.capture_once())
        self.assertEqual(service.last_reason, "frame_unavailable_or_expired")
        self.assertFalse(service.is_following())

    def test_initialization_phase_cannot_make_fresh_frames_future(self):
        original_mapping_failed = False
        for phase in (0, .001, .005, .010, .014, .015624):
            with self.subTest(initialization_phase=phase):
                clock, reader, port, service = self.create(phase)
                for frame_id, now in enumerate((100.016, 100.032, 100.048, 100.064), 1):
                    self.publish(clock, reader, frame_id, now)
                    packet = port.get_frame()
                    original_mapping_failed |= clock.wall() < packet.timestamp
                    self.assertEqual(service.wall() - packet.timestamp, 0.0)
                    self.assertTrue(service.capture_once())
        self.assertTrue(original_mapping_failed)





if __name__ == "__main__":
    unittest.main()
