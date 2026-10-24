"""Visible-target turning and fail-closed behavior; no device or network."""
import math
import unittest

from r3_follow.core import FollowController, MotionRequest, Observation, State


def observation(seq, now, error=0.0, size=.2, found=True, narrow=False):
    x, y, side = (error+1)*320, 240, size*640
    width = side*.5 if narrow else side
    corners = ((x-width/2, y-side/2), (x+width/2, y-side/2),
               (x+width/2, y+side/2), (x-width/2, y+side/2))
    return Observation(seq, now, "yaw-test", 640, 480, 0, found,
                       x if found else None, y if found else None,
                       size if found else None, corners if found else (),
                       "" if found else "not_found")


class VisibleYawTests(unittest.TestCase):
    def ready(self, **kwargs):
        c = FollowController(steering_mode="yaw", require_geometry=True,
                             recovery_window=3, **kwargs)
        c.observe(observation(0, 0))
        self.assertTrue(c.calibrate(0)[0])
        self.assertTrue(c.start(0)[0])
        return c

    def sample(self, c, seq, now, **kwargs):
        c.observe(observation(seq, now, **kwargs))
        return c.tick(now)

    def zero(self, request):
        self.assertEqual((request.right, request.forward, request.yaw), (0, 0, 0))

    def test_legacy_positional_request_and_default_controller_are_unchanged(self):
        request = MotionRequest(State.TRACKING, .1, -.2, "old", True)
        self.assertEqual((request.right, request.forward, request.reason, request.yaw),
                         (.1, -.2, "old", 0))
        c = FollowController()
        c.observe(observation(0, 0))
        c.calibrate(0)
        c.start(0)
        request = self.sample(c, 1, .1, error=.8, size=.1)
        self.assertEqual((request.right, request.forward, request.yaw), (.3, .3, 0))

    def test_turns_toward_visible_target_without_simultaneous_strafe(self):
        for error in (-.8, -.2, .2, .8):
            c = self.ready()
            request = self.sample(c, 1, .1, error=error)
            self.assertEqual(request.right, 0)
            self.assertGreater(request.yaw*error, 0)
            self.assertLessEqual(abs(request.yaw), .2)

    def test_yaw_deadzone_and_near_boundary_symmetry(self):
        for error in (-.12, -.05, 0, .05, .12):
            self.zero(self.sample(self.ready(), 1, .1, error=error))
        left = self.sample(self.ready(), 1, .1, error=-.14).yaw
        right = self.sample(self.ready(), 1, .1, error=.14).yaw
        self.assertAlmostEqual(left, -right)
        self.assertLess(abs(left), .6*.14)

    def test_forward_backward_limits_and_direction_remain_unchanged_when_aligned(self):
        for size, direction in ((.01, 1), (.19, 0), (.2, 0), (.4, -1)):
            request = self.sample(self.ready(), 1, .1, size=size)
            self.assertLessEqual(abs(request.forward), .3)
            self.assertEqual(request.yaw, 0)
            if direction:
                self.assertGreater(request.forward*direction, 0)
            else:
                self.assertEqual(request.forward, 0)

    def test_off_axis_depth_brakes_without_ramp_delay_and_has_hysteresis(self):
        for size in (.1, .4):
            c = self.ready(max_request_rate=.8)
            for seq in range(1, 6):
                self.sample(c, seq, seq*.1, size=size)
            self.assertGreater(abs(c.last_request.forward), .1)
            self.assertEqual(self.sample(c, 6, .501, error=.31, size=size).forward, 0)
            for seq, error in ((7, .29), (8, -.29), (9, .20)):
                self.assertEqual(self.sample(c, seq, seq*.1, error=error, size=size).forward, 0)
            request = self.sample(c, 10, 1., error=.18, size=size)
            self.assertGreater(abs(request.forward), 0)
            self.assertLessEqual(abs(request.forward), .080001)
            self.assertFalse(c.status()["steering"]["alignment_blocked"])

    def test_yaw_ramp_reversal_stops_old_direction_immediately(self):
        c = self.ready(max_request_rate=.8)
        for seq in range(1, 6):
            self.sample(c, seq, seq*.1, error=.7)
        self.assertAlmostEqual(c.last_request.yaw, .2)
        self.zero(self.sample(c, 6, .501, error=-.7))
        self.assertLess(self.sample(c, 7, .6, error=-.7).yaw, 0)
        self.zero(self.sample(c, 8, .601, error=0))

    def test_geometry_guard_still_blocks_depth_but_keeps_visible_turning(self):
        c = self.ready()
        request = self.sample(c, 1, .1, error=.2, size=.1, narrow=True)
        self.assertEqual(request.forward, 0)
        self.assertGreater(request.yaw, 0)
        self.assertTrue(c.distance_guard.blocked)
        for seq in (2, 3):
            self.assertEqual(self.sample(c, seq, seq*.1, size=.1).forward, 0)
        self.assertGreater(self.sample(c, 4, .4, size=.1).forward, 0)

    def test_stop_pause_loss_stale_invalid_all_clear_yaw_and_depth(self):
        for failure in ("stop", "pause", "loss", "stale", "invalid", "future", "duplicate"):
            c = self.ready(max_request_rate=.8)
            for seq in range(1, 6):
                self.sample(c, seq, seq*.1, error=.2, size=.1)
            self.assertGreater(c.last_request.yaw, 0)
            self.assertGreater(c.last_request.forward, 0)
            if failure in ("stop", "pause"):
                request = getattr(c, failure)()
            elif failure == "loss":
                request = self.sample(c, 6, .6, found=False)
            elif failure == "invalid":
                c.observe(None)
                request = c.tick(.6)
            elif failure == "future":
                c.observe(observation(6, 1.))
                request = c.tick(.6)
            elif failure == "duplicate":
                self.assertFalse(c.observe(observation(5, 1.1)))
                request = c.tick(1.1)
            else:
                request = c.tick(1.1)
            self.zero(request)

    def test_recovery_restarts_from_zero_and_stop_cancels_it(self):
        for cancel in (False, True):
            c = self.ready(max_request_rate=.8)
            self.sample(c, 1, .1, error=.7)
            self.zero(self.sample(c, 2, .2, found=False))
            if cancel:
                self.zero(c.stop())
            for seq in (3, 4, 5):
                self.zero(self.sample(c, seq, seq*.1, error=.7, size=.1))
            request = self.sample(c, 6, .6, error=.7, size=.1)
            if cancel:
                self.zero(request)
            else:
                self.assertEqual(c.state, State.TRACKING)
                self.assertGreater(request.yaw, 0)
                self.assertEqual(request.forward, 0)

    def test_pause_resume_keeps_distance_reference(self):
        c = self.ready()
        c.pause()
        for seq in (1, 2, 3):
            c.observe(observation(seq, seq*.1, size=.1))
        self.assertTrue(c.resume(.31)[0])
        self.assertEqual(c.reference_size, .2)
        self.assertGreater(c.last_request.forward, 0)

    def test_configuration_rejects_nonfinite_or_invalid_values(self):
        for name in ("yaw_limit", "alignment_stop", "alignment_resume"):
            for bad in (float("nan"), float("inf"), -1, 0, 1.01, True, "fast", None):
                with self.subTest(name=name, bad=bad), self.assertRaises(ValueError):
                    FollowController(**{name: bad})
        for mode in (None, "automatic", "", 1):
            with self.assertRaises(ValueError):
                FollowController(steering_mode=mode)
        with self.assertRaises(ValueError):
            FollowController(alignment_resume=.4, alignment_stop=.3)
        self.assertFalse(self.ready().status()["steering"]["physical_tuning_verified"])


class YawFeedbackTests(unittest.TestCase):
    def test_camera_projection_responds_to_turning_over_curve_and_latency_matrix(self):
        from turn_follow_check import run_scenario
        for response in (.1, .25, 1.):
            for delay in (0., .2, .4, .6):
                with self.subTest(response=response, delay=delay):
                    rows, summary = run_scenario(response=response, delay=delay)
                    self.assertEqual(summary["nontracking_nonzero"], 0)
                    self.assertTrue(summary["virtual_heading_changed"])
                    self.assertFalse(summary["physical_flight_verified"])
                    if delay <= .5:
                        self.assertEqual(summary["last_state"], "tracking")
                        self.assertTrue(all(r["target_visible_now"] for r in rows))
                        self.assertLess(abs(summary["final_visible_horizontal_error"]), .15)
                    else:
                        self.assertEqual(summary["last_state"], "frame_stale")
                        self.assertTrue(all(r["request_yaw"] == 0 for r in rows if r["t"] > 3))

    def test_closed_loop_loss_frozen_and_stop_are_deterministic(self):
        from turn_follow_check import run_scenario
        for scenario, state in (("short_occlusion", "tracking"),
                                ("out_of_view", "target_lost"),
                                ("frozen_frames", "frame_stale"),
                                ("stop_then_return", "stopped")):
            with self.subTest(scenario=scenario):
                rows, summary = run_scenario(scenario)
                self.assertEqual((rows, summary), run_scenario(scenario))
                self.assertEqual(summary["last_state"], state)
                self.assertEqual(summary["nontracking_nonzero"], 0)
                if scenario != "frozen_frames":
                    lost = next(r for r in rows if r["t"] == 5.)
                    self.assertEqual((lost["request_right"], lost["request_forward"], lost["request_yaw"]),
                                     (0, 0, 0))


if __name__ == "__main__":
    unittest.main()
