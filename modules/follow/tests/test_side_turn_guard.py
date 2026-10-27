"""Image-shape ambiguity must not become a forward/back motion request."""
from dataclasses import replace
import math
import unittest

from r3_follow.core import FollowController, Observation, State, marker_shape
from r3_follow.diagnostics import follow_hint
from r3_follow.mock_port import MockPort
from r3_follow.service import FollowService
from r3_follow.service_ports import Result


def rectangle(width=100, height=100, x=320, y=240, roll=0, shear=0):
    angle = math.radians(roll)
    ca, sa = math.cos(angle), math.sin(angle)
    points = []
    for px, py in ((-width/2, -height/2), (width/2, -height/2),
                   (width/2, height/2), (-width/2, height/2)):
        px += shear*py
        points.append((x+ca*px-sa*py, y+sa*px+ca*py))
    return tuple(points)


def observation(seq, t, corners=None, found=True):
    points = corners if corners is not None else rectangle()
    sides = [math.dist(points[i], points[(i+1) % 4]) for i in range(4)]
    return Observation(seq, t, "shape-test", 640, 480, 0, found,
                       sum(p[0] for p in points)/4,
                       sum(p[1] for p in points)/4, sum(sides)/4/640,
                       points if found else (), "" if found else "not_found")


class ShapeGuardTests(unittest.TestCase):
    def ready(self, **options):
        c = FollowController(require_geometry=True, **options)
        c.observe(observation(0, 0))
        self.assertTrue(c.calibrate(0)[0])
        self.assertTrue(c.start(0)[0])
        return c

    def test_same_depth_narrowing_cannot_create_forward_request(self):
        c = self.ready()
        for seq, roll in enumerate((0, 45, 90, 180), 1):
            obs = observation(seq, seq*.1, rectangle(50, 100, roll=roll))
            self.assertLess(obs.size_ratio, c.reference_size*.88)
            c.observe(obs)
            self.assertEqual(c.tick(seq*.1).forward, 0)
            self.assertEqual(c.state, State.TRACKING)

    def test_genuine_front_facing_distance_change_keeps_direction_and_gain(self):
        for width, expected in ((70, .18), (130, -.18)):
            c = self.ready()
            c.observe(observation(1, .1, rectangle(width, width)))
            self.assertAlmostEqual(c.tick(.1).forward, expected)
            self.assertFalse(c.distance_guard.blocked)

    def test_in_plane_rotation_does_not_trigger_side_turn_guard(self):
        for roll in (15, 45, 90, 179):
            c = self.ready()
            c.observe(observation(1, .1, rectangle(70, 70, roll=roll)))
            self.assertAlmostEqual(c.tick(.1).forward, .18)

    def test_diagonal_shear_is_rejected_even_with_similar_edge_lengths(self):
        c = self.ready()
        points = rectangle(60, 60, shear=.4)
        self.assertGreater(marker_shape(points)["axis_ratio"], .9)
        c.observe(observation(1, .1, points))
        self.assertEqual(c.tick(.1).forward, 0)
        self.assertTrue(c.distance_guard.blocked)

    def test_perspective_trapezoid_is_rejected(self):
        c = self.ready()
        points = ((295, 200), (345, 200), (365, 280), (275, 280))
        c.observe(observation(1, .1, points))
        self.assertEqual(c.tick(.1).forward, 0)

    def test_lateral_logic_continues_using_current_target(self):
        c = self.ready()
        for seq, x in enumerate((160, 480), 1):
            c.observe(observation(seq, seq*.1, rectangle(50, 100, x=x)))
            request = c.tick(seq*.1)
            self.assertAlmostEqual(request.right, -.3 if x < 320 else .3)
            self.assertEqual(request.forward, 0)

    def test_existing_forward_and_backward_requests_drop_without_ramp_delay(self):
        for side in (40, 160):
            c = self.ready(max_request_rate=.8)
            for seq in range(1, 6):
                c.observe(observation(seq, seq*.1, rectangle(side, side, x=480)))
                c.tick(seq*.1)
            self.assertGreater(abs(c.last_request.forward), .1)
            c.observe(observation(6, .51, rectangle(50, 100, x=480)))
            request = c.tick(.51)
            self.assertEqual(request.forward, 0)
            self.assertGreater(request.right, 0)

    def test_tilted_reference_is_rejected_and_old_reference_is_preserved(self):
        c = self.ready()
        old = c.reference_size
        c.pause()
        c.observe(observation(1, .1, rectangle(88, 100)))
        self.assertFalse(c.calibrate(.1)[0])
        self.assertEqual(c.reference_size, old)
        c.observe(observation(2, .2, rectangle(80, 80)))
        self.assertTrue(c.calibrate(.2)[0])
        self.assertAlmostEqual(c.reference_size, 80/640)

    def test_missing_geometry_is_not_a_valid_camera_reference(self):
        c = FollowController(require_geometry=True)
        c.observe(replace(observation(0, 0), corners=()))
        self.assertFalse(c.calibrate(0)[0])
        self.assertFalse(c.start(0)[0])

    def test_geometry_cannot_disappear_to_bypass_an_enabled_guard(self):
        c = self.ready()
        c.observe(replace(observation(1, .1, rectangle(60, 60)), corners=()))
        self.assertEqual(c.tick(.1).forward, 0)
        self.assertEqual(c.distance_guard.reason, "geometry_missing_or_invalid")

    def test_scalar_math_simulation_remains_explicitly_available(self):
        c = FollowController()
        c.observe(replace(observation(0, 0), corners=()))
        self.assertTrue(c.calibrate(0)[0])
        self.assertTrue(c.start(0)[0])
        c.observe(replace(observation(1, .1, rectangle(70, 70)), corners=()))
        self.assertAlmostEqual(c.tick(.1).forward, .18)
        c.observe(observation(2, .2, rectangle(50, 100)))
        c.tick(.2)
        c.observe(replace(observation(3, .3, rectangle(70, 70)), corners=()))
        self.assertEqual(c.tick(.3).forward, 0)
        self.assertTrue(c.distance_guard.enabled)

    def test_invalid_or_self_crossing_geometry_is_rejected(self):
        c = self.ready()
        invalid = ((), None, ((1, 2),), ((0, 0), (1, 1), (0, 1), (1, 0)),
                   ((0, 0), (100, 0), (float("nan"), 100), (0, 100)),
                   ((0, 0), (0, 0), (1, 1), (1, 1)))
        for seq, points in enumerate(invalid, 1):
            c.observe(replace(observation(seq, seq*.05, rectangle(60, 60)), corners=points))
            self.assertEqual(c.tick(seq*.05).forward, 0)
            self.assertIsNone(marker_shape(points))

    def test_reentry_requires_three_new_frames_and_time_not_repeated_ticks(self):
        c = self.ready(max_request_rate=.8)
        c.observe(observation(1, .1, rectangle(50, 100)))
        c.tick(.1)
        good = observation(2, .2, rectangle(60, 60))
        c.observe(good)
        for t in (.2, .25, .3, .35):
            self.assertFalse(c.observe(good))
            self.assertEqual(c.tick(t).forward, 0)
        c.observe(observation(3, .4, rectangle(60, 60)))
        self.assertEqual(c.tick(.4).forward, 0)  # only two new frames
        c.observe(observation(4, .5, rectangle(60, 60)))
        self.assertAlmostEqual(c.tick(.5).forward, .08)  # from zero, not old request

    def test_fast_frames_do_not_bypass_stable_duration(self):
        c = self.ready()
        c.observe(observation(1, .1, rectangle(50, 100)))
        c.tick(.1)
        for seq in range(2, 12):
            t = .1+seq*.01
            c.observe(observation(seq, t, rectangle(60, 60)))
            self.assertEqual(c.tick(t).forward, 0)
        c.observe(observation(12, .32, rectangle(60, 60)))
        self.assertGreater(c.tick(.32).forward, 0)

    def test_slow_or_interrupted_confirmation_does_not_unlock(self):
        c = self.ready()
        c.observe(observation(1, .1, rectangle(50, 100)))
        c.tick(.1)
        for seq, t in enumerate((.2, .5, .8, 1.1), 2):
            c.observe(observation(seq, t, rectangle(60, 60)))
            self.assertEqual(c.tick(t).forward, 0)

    def test_borderline_shape_chatter_does_not_pulse_depth(self):
        c = self.ready()
        c.observe(observation(1, .1, rectangle(50, 100)))
        c.tick(.1)
        for seq in range(2, 30):
            width = 84 if seq % 2 else 89
            c.observe(observation(seq, seq*.1, rectangle(width, 100)))
            self.assertEqual(c.tick(seq*.1).forward, 0)
        for seq in (30, 31, 32):
            c.observe(observation(seq, seq*.1, rectangle(70, 70)))
            request = c.tick(seq*.1)
        self.assertGreater(request.forward, 0)

    def test_stop_stays_stopped_when_shape_recovers(self):
        c = self.ready()
        c.observe(observation(1, .1, rectangle(50, 100)))
        c.tick(.1)
        c.stop()
        for seq in range(2, 8):
            c.observe(observation(seq, seq*.1, rectangle(60, 60, x=480)))
            request = c.tick(seq*.1)
            self.assertEqual((request.right, request.forward), (0, 0))
        self.assertEqual(c.state, State.STOPPED)

    def test_side_turn_guard_does_not_mask_loss_or_stale_frame(self):
        for failure in ("loss", "stale"):
            c = self.ready(recovery_window=3)
            c.observe(observation(1, .1, rectangle(50, 100, x=480)))
            c.tick(.1)
            if failure == "loss":
                c.observe(observation(2, .2, found=False))
            request = c.tick(.2 if failure == "loss" else .61)
            self.assertEqual((request.right, request.forward), (0, 0))
            self.assertEqual(c.state, State.RECOVERING if failure == "loss" else State.STALE)

    def test_hint_and_status_explain_why_depth_is_zero(self):
        c = self.ready()
        c.observe(observation(1, .1, rectangle(50, 100)))
        c.tick(.1)
        self.assertIn("前后请求保持零", follow_hint(c, True))
        self.assertFalse(c.status()["distance_guard"]["forward_allowed"])
        c.observe(observation(2, .2))
        self.assertIn("正在确认稳定", follow_hint(c, True))


class GeometryTracker:
    def detect(self, frame, frame_id, acquired_at, source):
        return replace(frame, frame_id=frame_id, acquired_at=acquired_at, source=source)


class ServiceShapeTests(unittest.TestCase):
    def setUp(self):
        self.t, self.seq = 10., 0
        self.port = MockPort(lambda: self.t)
        self.service = FollowService(self.port, tracker=GeometryTracker(),
            monotonic=lambda: self.t, wall_clock=lambda: 1800000000+self.t,
            steering_mode="lateral", log_directory=False)
        self.addCleanup(self.service.close)
        self.frame(rectangle())
        self.assertTrue(self.service.calibrate_reference().ok)
        self.assertTrue(self.service.start_follow().ok)
        for _ in range(5):
            self.frame(rectangle(50, 50, x=480))
            self.service.control_once()
        self.assertEqual(self.port.velocity, (30, 30, 0, 0))

    def frame(self, points, advance=.1):
        self.t += advance
        self.seq += 1
        self.port.publish(observation(self.seq, self.t, points), self.seq, 1800000000+self.t)
        self.assertTrue(self.service.capture_once())

    def test_depth_brake_bypasses_send_interval_without_increasing_lateral(self):
        previous = self.service.last_send
        self.frame(rectangle(50, 100, x=480), advance=.01)
        self.service.control_once()
        self.assertEqual(self.port.velocity, (30, 0, 0, 0))
        self.assertAlmostEqual(self.service.last_send-previous, .01)
        motion = next(event for event in reversed(self.service.events)
                      if event['event'] == 'motion_request')
        self.assertTrue(motion["urgent_depth_stop"])
        n = len(self.port.events)
        for _ in range(5):
            self.service.control_once()
        self.assertEqual(len(self.port.events), n)

    def test_brake_rejection_stops_follow_and_releases_ownership(self):
        self.frame(rectangle(50, 100, x=480), advance=.01)
        from unittest.mock import patch
        with patch.object(self.port, "update_velocity", return_value=Result("failed", "injected")):
            self.service.control_once()
        self.assertEqual(self.port.velocity, (0, 0, 0, 0))
        self.assertIsNone(self.port.lease)
        self.assertEqual(self.service.controller.state, State.STOPPED)

    def test_lost_ownership_cannot_brake_the_new_owner(self):
        other = self.port.revoke_for_test("model")
        self.port.update_velocity(other, left_right=0, forward_back=9, up_down=0, yaw=0)
        self.frame(rectangle(50, 100, x=480), advance=.01)
        self.service.control_once()
        self.assertEqual(self.port.velocity, (0, 9, 0, 0))
        self.assertEqual(self.service.controller.state, State.STOPPED)


if __name__ == "__main__":
    unittest.main()
