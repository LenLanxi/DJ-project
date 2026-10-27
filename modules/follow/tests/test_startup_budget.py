"""A first-frame timeout must not prematurely end the SDK startup wait."""
import unittest
from r3_follow.core import Observation
from r3_follow.mock_port import MockPort
from r3_follow.service import FollowService


class Detector:
    reason = 'detection_budget_exceeded'
    def detect(self, frame, frame_id, acquired_at, source):
        found = not self.reason
        return Observation(frame_id, acquired_at, source, 640, 480, 0, found,
            320 if found else None, 240 if found else None, .25 if found else None,
            corners=((240,160),(400,160),(400,320),(240,320)) if found else (),
            reason=self.reason)


class StartupBudgetTests(unittest.TestCase):
    def setUp(self):
        self.now = 10.
        self.port = MockPort(lambda: self.now)
        self.detector = Detector()
        self.service = FollowService(self.port, tracker=self.detector,
            monotonic=lambda: self.now, wall_clock=lambda: self.now,
            log_directory=False)
        self.addCleanup(self.service.close)
        self.seq = 0

    def capture(self):
        self.seq += 1
        self.now += .05
        self.port.publish('fixture', self.seq, self.now)
        self.assertTrue(self.service.capture_once())

    def test_initial_timeout_waits_then_new_valid_frame_calibrates(self):
        for reason in ('detection_budget_exceeded', 'ambiguous_scan_budget_exceeded'):
            self.detector.reason = reason
            self.capture()
            self.assertFalse(self.service.frame_ready)
            self.assertFalse(self.service.calibrate_reference().ok)
            self.assertIsNone(self.port.lease)
        self.detector.reason = ''
        self.capture()
        self.assertTrue(self.service.frame_ready)
        self.assertTrue(self.service.calibrate_reference().ok)

    def test_confirmed_duplicate_still_fails_calibration_immediately(self):
        self.detector.reason = 'ambiguous_duplicate_id'
        self.capture()
        self.assertTrue(self.service.frame_ready)
        self.assertFalse(self.service.calibrate_reference().ok)

    def test_calibrated_loss_keeps_existing_recovery_path(self):
        self.detector.reason = ''
        self.capture()
        self.assertTrue(self.service.calibrate_reference().ok)
        self.assertTrue(self.service.start_follow().ok)
        self.detector.reason = 'detection_budget_exceeded'
        self.capture()
        self.service.control_once()
        self.assertTrue(self.service.frame_ready)
        self.assertEqual(self.port.velocity, (0,0,0,0))
        self.assertFalse(self.service.is_following())
