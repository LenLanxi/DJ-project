"""Behavior checks for boundary noise and the actual background output path."""
import unittest

from r3_follow.core import FollowController, Observation
from r3_follow.mock_port import MockPort
from r3_follow.service import FollowService


def observation(seq, t, x_error=0., size_error=0., found=True):
    return Observation(seq, t, 'test', 640, 480, 0, found,
                       320*(1+x_error) if found else None,
                       240 if found else None,
                       .25*(1-size_error) if found else None)


class StabilityTests(unittest.TestCase):
    def ready(self, **kwargs):
        c = FollowController(**kwargs)
        c.observe(observation(0, 0))
        self.assertTrue(c.calibrate(0)[0])
        self.assertTrue(c.start(0)[0])
        return c

    def test_deadzone_edge_is_continuous_in_both_axes_and_directions(self):
        for sign in (-1, 1):
            c = self.ready()
            for i, error in enumerate((.119999, .12, .120001), 1):
                c.observe(observation(i, i*.1, sign*error, sign*error))
                request = c.tick(i*.1)
                self.assertLess(max(abs(request.right), abs(request.forward)), .00001)

    def test_one_pixel_boundary_noise_does_not_produce_large_channel_pulses(self):
        c = self.ready(max_request_rate=.8)
        for i in range(1, 61):
            # +/- 0.64 image pixels around the old deadzone threshold.
            error = .118 if i % 2 else .122
            c.observe(observation(i, i*.1, error, error))
            request = c.tick(i*.1)
            self.assertLessEqual(abs(round(request.right*100)), 1)
            self.assertLessEqual(abs(round(request.forward*100)), 1)

    def test_saturation_is_unchanged_and_both_axes_remain_symmetric(self):
        c = self.ready()
        for i, error in enumerate((-.9, -.5, .5, .9), 1):
            c.observe(observation(i, i*.1, error, error))
            request = c.tick(i*.1)
            expected = -.3 if error < 0 else .3
            self.assertAlmostEqual(request.right, expected)
            self.assertAlmostEqual(request.forward, expected)

    def test_boundary_smoothing_does_not_hold_motion_after_loss(self):
        c = self.ready(max_request_rate=.8, recovery_window=3)
        for i in range(1, 6):
            c.observe(observation(i, i*.1, .8, .8))
            c.tick(i*.1)
        self.assertEqual(c.last_request.right, .3)
        c.observe(observation(6, .6, found=False))
        request = c.tick(.6)
        self.assertEqual((request.right, request.forward), (0, 0))

    def test_response_outside_transition_keeps_original_gain(self):
        c = self.ready()
        for i, error in enumerate((-.45, -.3, -.16, .16, .3, .45), 1):
            c.observe(observation(i, i*.05, error, error))
            request = c.tick(i*.05)
            self.assertAlmostEqual(request.right, .6*error)
            self.assertAlmostEqual(request.forward, .6*error)


class PassthroughTracker:
    def detect(self, frame, frame_id, acquired_at, source):
        from dataclasses import replace
        obs = observation(frame_id, acquired_at, *frame)
        if not obs.found:
            return obs
        half = obs.size_ratio*obs.width/2
        x, y = obs.center_x, obs.center_y
        return replace(obs, corners=((x-half,y-half),(x+half,y-half),
                                     (x+half,y+half),(x-half,y+half)))


class ServiceStabilityTests(unittest.TestCase):
    def setUp(self):
        self.t = 0.
        self.seq = 0
        self.port = MockPort(lambda: self.t)
        self.service = FollowService(self.port, tracker=PassthroughTracker(),
            monotonic=lambda: self.t, wall_clock=lambda: 1800000000+self.t,
            recovery_window=3)
        self.addCleanup(self.service.close)
        self.frame(0, 0, 0)
        self.assertTrue(self.service.calibrate_reference().ok)

    def frame(self, t, x=.8, size=.8, found=True):
        self.t = t
        self.seq += 1
        self.port.publish((x, size, found), self.seq, 1800000000+t)
        self.assertTrue(self.service.capture_once())

    def moving(self):
        self.assertTrue(self.service.start_follow().ok)
        for i in range(1, 6):
            self.frame(i*.1)
            self.service.control_once()
        self.assertEqual(self.port.velocity, (30, 30, 0, 0))

    def test_background_start_and_both_channels_ramp_from_zero(self):
        self.frame(.1)
        self.assertTrue(self.service.start_follow().ok)
        self.assertEqual(self.port.velocity, (0, 0, 0, 0))
        previous = 0
        for i in range(1, 6):
            self.frame(.1+i*.1)
            self.service.control_once()
            right, forward, _, _ = self.port.velocity
            self.assertLessEqual(right-previous, 8)
            self.assertEqual(right, forward)
            previous = right
        self.assertEqual(previous, 30)

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
        result, ticket = self.service.pause_for_action()
        self.assertTrue(result.ok)
        lease = self.port.request_control('model').lease
        self.frame(.6)
        self.port.release_control(lease)
        self.assertTrue(self.service.finish_action(ticket, 'completed').ok)
        self.assertEqual(self.port.velocity, (0, 0, 0, 0))
        self.frame(.7)
        self.service.control_once()
        self.assertEqual(self.port.velocity, (8, 0, 0, 0))  # depth waits for stable new geometry
        self.frame(.8)
        self.service.control_once()
        self.assertEqual(self.port.velocity, (16, 8, 0, 0))
        self.frame(.9)
        self.service.control_once()
        self.assertEqual(self.port.velocity, (24, 16, 0, 0))
        self.service.stop_follow()
        self.assertFalse(self.service.finish_action(ticket, 'completed').ok)
        self.assertEqual(self.port.velocity, (0, 0, 0, 0))


if __name__ == '__main__':
    unittest.main()
