from dataclasses import replace
import math
import unittest

import cv2
import numpy as np
from r3_follow.core import FollowController, Observation, State
from r3_follow.vision import MarkerTracker, synthetic_frame
from r3_follow.scenario import run
import tempfile
from pathlib import Path


def obs(seq=1, at=10, x=320, size=.25, found=True):
    return Observation(seq, at, "test", 640, 480, 0, found,
                       x if found else None, 240 if found else None, size if found else None)


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.c = FollowController()
        self.c.observe(obs())

    def arm(self):
        self.assertTrue(self.c.calibrate(10)[0])
        self.assertTrue(self.c.start(10)[0])

    def zero(self, request):
        self.assertEqual((request.right, request.forward), (0, 0))

    def test_default_inactive_and_requires_calibration(self):
        self.zero(self.c.tick(10))
        self.assertFalse(self.c.start(10)[0])
        self.zero(self.c.last_request)

    def test_center_deadband_and_bound(self):
        self.arm()
        self.zero(self.c.tick(10))
        self.c.observe(obs(2, 10.1, x=340, size=.27))
        self.zero(self.c.tick(10.1))
        self.c.observe(obs(3, 10.2, x=630, size=.05))
        req = self.c.tick(10.2)
        self.assertGreater(req.right, 0)
        self.assertGreater(req.forward, 0)
        self.assertLessEqual(max(abs(req.right), abs(req.forward)), .3)

    def test_direction_signs(self):
        self.arm()
        self.c.observe(obs(2, 10.1, x=100, size=.4))
        req = self.c.tick(10.1)
        self.assertLess(req.right, 0)
        self.assertLess(req.forward, 0)

    def test_pause_resume_stop(self):
        self.arm()
        self.zero(self.c.pause())
        self.c.observe(obs(2, 10.1, x=600))
        self.zero(self.c.tick(10.1))
        self.assertTrue(self.c.resume(10.1)[0])
        self.assertGreater(self.c.tick(10.1).right, 0)
        self.zero(self.c.stop())
        self.c.observe(obs(3, 10.2, x=600))
        self.zero(self.c.tick(10.2))
        self.assertFalse(self.c.resume(10.2)[0])

    def test_loss_is_immediate_and_latched(self):
        self.arm()
        self.c.observe(obs(2, 10.1, found=False))
        self.zero(self.c.tick(10.1))
        self.assertEqual(self.c.state, State.LOST)
        self.c.observe(obs(3, 10.2, x=600))
        self.zero(self.c.tick(10.2))
        self.assertTrue(self.c.start(10.2)[0])

    def test_default_freshness_is_one_second_from_acquisition(self):
        self.arm()
        self.assertEqual(self.c.max_age, 1.0)
        for now in (10.5, 10.6, 10.99, 11.0):
            with self.subTest(now=now):
                self.assertEqual(self.c.tick(now).state, State.TRACKING)
        self.assertEqual(self.c.tick(11.01).state, State.STALE)
        self.zero(self.c.last_request)

    def test_watchdog_without_new_frames(self):
        self.arm()
        self.zero(self.c.tick(11.1))
        self.assertEqual(self.c.state, State.STALE)

    def test_duplicate_does_not_refresh(self):
        self.arm()
        self.assertFalse(self.c.observe(obs(1, 11.1, x=600)))
        self.zero(self.c.tick(11.1))
        self.assertEqual(self.c.state, State.STALE)

    def test_reordered_frame_ignored(self):
        self.c.observe(obs(3, 10.1))
        self.assertFalse(self.c.observe(obs(2, 10.2, x=600)))
        self.assertEqual(self.c.observation.frame_id, 3)

    def test_future_frame_blocks_start(self):
        self.c.observe(obs(2, 11))
        self.assertFalse(self.c.start(10)[0])

    def test_invalid_values_stop_and_require_restart(self):
        for invalid in [None, replace(obs(), center_x=math.nan),
                        replace(obs(), width=0), replace(obs(), size_ratio=-1),
                        replace(obs(), target_id=1), replace(obs(), source="other"),
                        replace(obs(), acquired_at=math.inf)]:
            c = FollowController()
            c.observe(obs())
            c.calibrate(10)
            c.start(10)
            self.assertFalse(c.observe(invalid))
            self.zero(c.tick(10.1))
            self.assertEqual(c.state, State.INVALID)
            c.observe(obs(2, 10.2))
            self.zero(c.tick(10.2))

    def test_time_regression_invalid(self):
        self.arm()
        self.assertFalse(self.c.observe(obs(2, 9)))
        self.zero(self.c.tick(10))

    def test_no_calibration_while_tracking(self):
        self.arm()
        self.assertFalse(self.c.calibrate(10)[0])

    def test_resume_rechecks_freshness(self):
        self.arm()
        self.c.pause()
        self.assertFalse(self.c.resume(11.01)[0])
        self.zero(self.c.last_request)


class VisionTests(unittest.TestCase):
    def setUp(self):
        self.t = MarkerTracker()

    def detect(self, frame):
        return self.t.detect(frame, 1, 10, "test")

    def test_generated_target_and_rotation(self):
        image = synthetic_frame()
        for sample in (image, cv2.rotate(image, cv2.ROTATE_90_CLOCKWISE)):
            result = self.detect(sample)
            self.assertTrue(result.found)
            self.assertEqual(result.target_id, 0)
            self.assertEqual(len(result.corners), 4)

    def test_wrong_target_and_blank(self):
        self.assertFalse(self.detect(synthetic_frame(marker_id=1)).found)
        self.assertFalse(self.detect(synthetic_frame(visible=False)).found)

    def test_duplicate_target_rejected(self):
        a = synthetic_frame(x=.2, size=.12)
        b = synthetic_frame(x=.8, size=.12)
        result = self.detect(np.minimum(a, b))
        self.assertFalse(result.found)
        self.assertEqual(result.reason, "ambiguous_duplicate_id")

    def test_loss_clears_coordinates(self):
        self.assertTrue(self.detect(synthetic_frame()).found)
        lost = self.detect(synthetic_frame(visible=False))
        self.assertIsNone(lost.center_x)
        self.assertIsNone(lost.size_ratio)

    def test_invalid_image(self):
        for frame in [None, np.zeros((0, 0), np.uint8), np.zeros((10, 10), np.float32)]:
            with self.assertRaises(ValueError):
                self.detect(frame)

    def test_size_position_change(self):
        left = self.detect(synthetic_frame(x=.25, size=.15))
        right = self.detect(synthetic_frame(x=.75, size=.3))
        self.assertTrue(left.found and right.found)
        self.assertLess(left.center_x, right.center_x)
        self.assertLess(left.size_ratio, right.size_ratio)

    def test_scenario(self):
        with tempfile.TemporaryDirectory() as folder:
            dest = Path(folder) / "scenario.jsonl"
            rows = run(dest)
            self.assertEqual(len(rows), 13)
            self.assertEqual(len(dest.read_text(encoding="utf-8").splitlines()), 13)


if __name__ == "__main__":
    unittest.main()
