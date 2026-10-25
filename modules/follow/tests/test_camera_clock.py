"""Local camera clock regressions; no camera, GUI, network, or log files."""
import math
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import cv2
import numpy as np
from camera_pid_bench import make_service, save_diagnostic_frame
from r3_follow.core import Observation
from r3_follow.vision import synthetic_frame


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
        self.assertTrue(service.start_follow().ok)

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
        self.assertEqual(service.get_follow_status()["state"], "tracking")

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

    def test_manual_diagnostic_saves_latest_raw_pixels_and_matching_frame_id(self):
        reader=Reader()
        frame=synthetic_frame(x=.7)
        reader.latest=(42,123.5,frame)
        previous=Observation(41,123.4,'test',640,480,0,False,reason='not_found')
        with TemporaryDirectory() as temporary:
            result=save_diagnostic_frame(reader,previous,Path(temporary)/'diagnostic_frames',
                                         wall=lambda:1800000123.75)
            self.assertTrue(result['ok'])
            self.assertIn('第42帧',result['message'])
            self.assertIn('不是这张图的识别结果',result['message'])
            pixels=cv2.imdecode(np.frombuffer(Path(result['image_path']).read_bytes(),dtype=np.uint8),
                                cv2.IMREAD_UNCHANGED)
            np.testing.assert_array_equal(pixels,frame)
            record=json.loads(Path(result['metadata_path']).read_text(encoding='utf-8'))
            self.assertEqual(record['frame_id'],42)
            self.assertEqual(record['capture_monotonic'],123.5)
            self.assertEqual(record['saved_at'],1800000123.75)
            self.assertEqual(record['latest_detection_frame_id'],41)
            self.assertEqual(record['latest_detection_reason'],'not_found')
            self.assertFalse(record['detection_matches_saved_frame'])

    def test_manual_diagnostic_without_frame_creates_no_files(self):
        with TemporaryDirectory() as temporary:
            directory=Path(temporary)/'diagnostic_frames'
            result=save_diagnostic_frame(Reader(),directory=directory)
            self.assertFalse(result['ok'])
            self.assertIn('没有可保存',result['message'])
            self.assertFalse(directory.exists())

    def test_manual_diagnostic_encoding_failure_creates_no_files(self):
        reader=Reader()
        reader.latest=(42,123.5,synthetic_frame())
        with TemporaryDirectory() as temporary, patch('cv2.imencode',return_value=(False,None)):
            directory=Path(temporary)/'diagnostic_frames'
            result=save_diagnostic_frame(reader,directory=directory)
            self.assertFalse(result['ok'])
            self.assertIn('PNG 编码失败',result['message'])
            self.assertFalse(directory.exists())


if __name__ == "__main__":
    unittest.main()
