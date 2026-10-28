"""Bad frame metadata stops motion and leaves an actionable diagnostic."""
import unittest

from r3_follow.core import Observation
from r3_follow.mock_port import MockPort
from r3_follow.service import FollowService


class FrameRejectionTests(unittest.TestCase):
    def test_missing_stale_future_and_expired_are_distinguishable_and_zero(self):
        for kind in ("missing_pixels", "source_stale", "future_timestamp", "expired_timestamp"):
            with self.subTest(kind=kind):
                now = [10.]
                mono = lambda: now[0]
                wall = lambda: 1800000000. + now[0]
                port = MockPort(mono)

                class Detector:
                    def detect(self, frame, frame_id, acquired_at, source):
                        return Observation(frame_id, acquired_at, source, 640, 480, 0,
                            True, frame, 240, .25,
                            corners=((frame-80, 160), (frame+80, 160),
                                     (frame+80, 320), (frame-80, 320)))

                service = FollowService(port, tracker=Detector(), monotonic=mono,
                    wall_clock=wall, steering_mode="lateral", log_directory=False)
                try:
                    port.publish(320, 1, wall())
                    self.assertTrue(service.capture_once())
                    self.assertTrue(service.calibrate_reference().ok)
                    self.assertTrue(service.start_follow().ok)
                    now[0] += .1
                    port.publish(550, 2, wall())
                    self.assertTrue(service.capture_once())
                    service.control_once()
                    self.assertNotEqual(port.velocity, (0, 0, 0, 0))
                    now[0] += 1.2
                    stamp = wall() + (.01 if kind == "future_timestamp" else
                                      -1.1 if kind == "expired_timestamp" else 0.)
                    port.publish(None if kind == "missing_pixels" else 550,
                                 3, stamp, stale=kind == "source_stale")
                    self.assertFalse(service.capture_once())
                    self.assertEqual(port.velocity, (0, 0, 0, 0))
                    self.assertFalse(service.is_following())
                    events = [e for e in service.events if e["event"] == "input_frame_rejected"]
                    self.assertEqual(len(events), 1)
                    self.assertEqual(events[0]["reason"], kind)
                    self.assertEqual(events[0]["frame_id"], 3)
                    self.assertAlmostEqual(events[0]["frame_age_ms"], (wall()-stamp)*1000)
                    self.assertFalse(service.start_follow().ok)
                finally:
                    service.close()


if __name__ == "__main__":
    unittest.main()
