from tests.helpers.FollowFixture import make_controller as FollowController
from follow.r3_follow.config import FollowConfig
"""Behavior checks for boundary noise and the actual background output path."""
import unittest

from follow.r3_follow.core import Observation
from tests.helpers.FollowFixture import FakeDevice
from follow.r3_follow import FollowService


def observation(seq, t, x_error=0., size_error=0., found=True):
    return Observation(seq, t, 'test', 640, 480, 0, found,
                       320*(1+x_error) if found else None,
                       240 if found else None,
                       .25*(1-size_error) if found else None)


class PassthroughTracker:
    def detect(self, frame, frame_id, acquired_at, source):
        x, size, found = frame
        from dataclasses import replace
        obs = observation(frame_id, acquired_at, x, size, found)
        if not found:
            return replace(obs, source=source)
        half=obs.size_ratio*640/2
        return replace(obs, source=source, corners=((obs.center_x-half,240-half),
            (obs.center_x+half,240-half),(obs.center_x+half,240+half),(obs.center_x-half,240+half)))


class ServiceStabilityTests(unittest.TestCase):
    def setUp(self):
        self.t = 0.
        self.seq = 0
        self.port = FakeDevice(lambda: self.t)
        self.service = FollowService(self.port, config=FollowConfig(steering_mode='lateral', max_request_rate=.8),
                           tracker=PassthroughTracker(),
                           monotonic=lambda: self.t,
                           wall_clock=lambda: 1800000000 + self.t,
                           recovery_window=3,
                           log_directory=False)
        self.addCleanup(self.service.close)
        self.frame(0, 0, 0)
        self.assertTrue(self.service.calibrate_reference().ok)

    def frame(self, t, x=.8, size=.8, found=True):
        self.t = t
        self.seq += 1
        self.port.publish((x, size, found), self.seq, 1800000000+t)
        self.assertTrue(self.service.capture_once())

    def moving(self):
        self.assertTrue(self.service._start_tracking().ok)
        for i in range(1, 6):
            self.frame(i*.1)
            self.service.control_once()
        self.assertEqual(self.port.velocity, (20, 30, 0, 0))

    def test_background_start_and_both_channels_ramp_from_zero(self):
        self.frame(.1)
        self.assertTrue(self.service._start_tracking().ok)
        self.assertEqual(self.port.velocity, (0, 0, 0, 0))
        previous = 0
        for i in range(1, 6):
            self.frame(.1+i*.1)
            self.service.control_once()
            right, forward, _, _ = self.port.velocity
            self.assertLessEqual(right-previous, 8)
            self.assertLessEqual(right, forward)
            previous = right
        self.assertEqual(previous, 20)

    def test_background_reversal_brakes_then_ramps(self):
        self.moving()
        self.frame(.6, -.8, -.8)
        self.service.control_once()
        self.assertEqual(self.port.velocity, (0, 0, 0, 0))
        self.frame(.7, -.8, -.8)
        self.service.control_once()
        self.assertEqual(self.port.velocity, (-8, -8, 0, 0))

    def test_background_loss_is_zero_even_before_next_send_deadline(self):
        self.moving()
        self.frame(.51, found=False)
        self.service.control_once()
        self.assertEqual(self.port.velocity, (0, 0, 0, 0))

    def test_handoff_recovery_ramps_and_old_completion_stays_cancelled(self):
        self.moving()
        ticket = self.service.pause_if_active()
        self.assertIsNotNone(ticket)
        lease = (self.port.request_control('model'), self.port.lease)[1]
        self.frame(.6)
        self.port.release_control(lease)
        self.assertTrue(self.service.finish_action(ticket, True).ok)
        self.assertEqual(self.port.velocity, (0, 0, 0, 0))
        self.frame(.7)
        self.service.control_once()
        self.assertEqual(self.port.velocity, (8, 0, 0, 0))  # depth waits for stable new geometry
        self.frame(.8)
        self.service.control_once()
        self.assertEqual(self.port.velocity, (16, 8, 0, 0))
        self.frame(.9)
        self.service.control_once()
        self.assertEqual(self.port.velocity, (20, 16, 0, 0))
        self.service._stop_tracking()
        self.assertFalse(self.service.finish_action(ticket, True).ok)
        self.assertEqual(self.port.velocity, (0, 0, 0, 0))


if __name__ == '__main__':
    unittest.main()
