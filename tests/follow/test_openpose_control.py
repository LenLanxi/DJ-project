from tests.helpers.FollowFixture import make_controller as FollowController
from follow.r3_follow.config import FollowConfig
"""Open-source equation parity and R3 outer guard integration; no devices."""
from dataclasses import replace
import hashlib
import json
import math
from pathlib import Path
import unittest

from follow.r3_follow._vendor.simple_pid import PID
from follow.r3_follow.core import Observation, State
from follow.r3_follow.openpose_control import (OpenPoseMarkerControl, YAW_DERIVATIVE_LIMIT,
                                        YAW_DERIVATIVE_MIN_DT, YAW_GAINS)


from FollowFixtures import control_observation as observation


class OpenPoseAdapterTests(unittest.TestCase):
    def call(self, adapter, seq=0, timestamp=0., **changes):
        values = dict(frame_id=seq, acquired_at=timestamp, center_x=400., image_width=640,
                      size_ratio=.1, reference_size=.2)
        values.update(changes)
        return adapter.update(**values)



    def test_resolution_normalization_preserves_same_marker_inputs(self):
        requests = [self.call(OpenPoseMarkerControl(FollowConfig()), image_width=w, center_x=w*.7)
                    for w in (640, 960, 1280, 1920)]
        self.assertTrue(all(r == requests[0] for r in requests))

    def test_both_axes_move_together_at_large_horizontal_offset(self):
        for x, yaw_sign in ((.1, -1), (.9, 1)):
            for size, pitch_sign in ((.1, 1), (.3, -1)):
                request = self.call(OpenPoseMarkerControl(FollowConfig()), center_x=x*640, size_ratio=size)
                self.assertEqual(request[0], 0)
                self.assertGreater(request[1]*pitch_sign, 0)
                self.assertGreater(request[2]*yaw_sign, 0)
                self.assertLessEqual(abs(request[1]), .3)
                self.assertLessEqual(abs(request[2]), .2)

    def test_duplicate_and_equal_time_frames_do_not_integrate(self):
        a = OpenPoseMarkerControl(FollowConfig())
        original = self.call(a, size_ratio=.16)
        components = a.pitch_pid.components
        for index in range(20):
            self.assertEqual(self.call(a, 0, index*.01, size_ratio=.16), original)
            self.assertEqual(a.pitch_pid.components, components)
        self.assertEqual(self.call(a, 1, 0., size_ratio=.16), original)
        self.assertEqual(a.pitch_pid.components, components)
        self.call(a, 2, .1, size_ratio=.16)
        self.assertNotEqual(a.pitch_pid.components, components)

    def test_actual_capture_interval_used_and_tiny_interval_is_not_fabricated(self):
        a = OpenPoseMarkerControl(FollowConfig())
        self.call(a, size_ratio=.16)
        before = a.pitch_pid.components
        self.call(a, 1, .0001, size_ratio=.19)
        self.assertEqual(a.pitch_pid.components, before)
        self.call(a, 2, .037, size_ratio=.16)
        self.assertEqual(a.last_dt, .037)
        self.assertAlmostEqual(a.pitch_pid.components[1], .01*25.6*.037)

    def test_frame_gap_resets_integral_and_derivative_history(self):
        a = OpenPoseMarkerControl(FollowConfig())
        for i in range(5):
            self.call(a, i, i*.1, size_ratio=.1+i*.005)
        after_gap = self.call(a, 5, 1., size_ratio=.18)
        fresh = self.call(OpenPoseMarkerControl(FollowConfig()), 5, 1., size_ratio=.18)
        self.assertEqual(after_gap, fresh)
        self.assertIsNone(a.last_dt)

    def test_shape_block_clears_pitch_history_and_keeps_yaw(self):
        a = OpenPoseMarkerControl(FollowConfig())
        for i in range(5):
            self.call(a, i, i*.1)
        blocked = self.call(a, 5, .5, forward_allowed=False)
        self.assertEqual(blocked[1], 0)
        self.assertGreater(blocked[2], 0)
        self.assertEqual(a.pitch_pid.components, (0, 0, 0))
        restored = self.call(a, 6, .6, size_ratio=.18)
        fresh = self.call(OpenPoseMarkerControl(FollowConfig()), 6, .6, size_ratio=.18)
        self.assertEqual(restored, fresh)

    def test_bad_input_resets_and_raises_instead_of_nonfinite_request(self):
        bad = [(key, value) for key in ("acquired_at", "center_x", "size_ratio", "reference_size")
               for value in (float("nan"), float("inf"), True)]
        bad += [("image_width", 0), ("size_ratio", 0), ("reference_size", 0),
                ("frame_id", True), ("center_x", 640)]
        for key, value in bad:
            with self.subTest(key=key, value=value):
                a = OpenPoseMarkerControl(FollowConfig())
                self.call(a)
                with self.assertRaises(ValueError):
                    self.call(a, 1, .1, **{key: value})
                self.assertEqual(a.status()["raw_normalized_request"], (0, 0, 0))

    def test_reset_removes_old_tracking_integral(self):
        a = OpenPoseMarkerControl(FollowConfig())
        for i in range(30):
            self.call(a, i, i*.1)
        self.assertNotEqual(a.pitch_pid.components[1], 0)
        a.reset()
        self.assertEqual(self.call(a, 40, 4.), self.call(OpenPoseMarkerControl(FollowConfig()), 40, 4.))


class OpenPoseControllerTests(unittest.TestCase):
    def ready(self, **kwargs):
        c = FollowController(config=FollowConfig(control_profile="openpose_pid", steering_mode="yaw", max_request_rate=None), require_geometry=True, recovery_window=3., **kwargs)
        c.observe(observation(0, 0.))
        self.assertTrue(c.calibrate(0.)[0])
        self.assertTrue(c.start(0.)[0])
        return c

    def sample(self, c, seq, timestamp, **kwargs):
        c.observe(observation(seq, timestamp, **kwargs))
        return c.tick(timestamp)

    def zero(self, request):
        self.assertEqual((request.right, request.forward, request.yaw), (0., 0., 0.))

    def test_new_profile_keeps_simultaneous_depth_and_yaw_with_old_caps(self):
        c = self.ready()
        request = self.sample(c, 1, .1, x=.8, size=.1)
        self.assertEqual((request.forward, request.yaw), (.3, .2))
        self.assertEqual(request.right, 0.)
        self.assertEqual(c.status()["steering"]["mode"], "yaw")

    def test_shape_ambiguity_still_stops_depth_immediately(self):
        c = self.ready(max_request_rate=.8)
        for seq in range(1, 8):
            self.sample(c, seq, seq*.1, x=.8, size=.1)
        self.assertAlmostEqual(c.last_request.forward, .3)
        request = self.sample(c, 8, .71, x=.8, size=.1, narrow=True)
        self.assertEqual(request.forward, 0)
        self.assertGreater(request.yaw, 0)
        self.assertEqual(c.status()["openpose_control"]["pitch_components"], (0, 0, 0))

    def test_same_frame_ticks_only_advance_existing_ramp_not_pid(self):
        c = self.ready(max_request_rate=.8)
        first = self.sample(c, 1, .1, x=.8, size=.1)
        self.assertAlmostEqual(first.forward, .08)
        components = c.status()["openpose_control"]["pitch_components"]
        for time in (.12, .14, .16, .18):
            c.tick(time)
            self.assertEqual(c.status()["openpose_control"]["pitch_components"], components)
        self.assertGreater(c.last_request.forward, first.forward)
        self.assertLessEqual(c.last_request.forward-first.forward, .8*.08+1e-12)

    def test_stop_pause_loss_stale_invalid_clear_output_and_pid(self):
        for mode in ("stop", "pause", "loss", "stale", "invalid", "wrong_id"):
            with self.subTest(mode=mode):
                c = self.ready(max_request_rate=.8)
                for seq in range(1, 8):
                    self.sample(c, seq, seq*.1, x=.8, size=.1)
                if mode in ("stop", "pause"):
                    request = getattr(c, mode)()
                elif mode == "loss":
                    request = self.sample(c, 8, .8, found=False)
                elif mode == "stale":
                    request = c.tick(1.8)
                else:
                    c.observe(None if mode == "invalid" else
                              replace(observation(8, .8), target_id=1))
                    request = c.tick(.8)
                self.zero(request)
                pid = c.status()["openpose_control"]
                self.assertEqual(pid["pitch_components"], (0, 0, 0))
                self.assertEqual(pid["yaw_components"], (0, 0, 0))

    def test_pause_resume_and_calibrate_keep_or_replace_reference_but_reset_history(self):
        c = self.ready()
        for seq in range(1, 5):
            self.sample(c, seq, seq*.1, size=.1)
        c.pause()
        for seq in range(5, 8):
            c.observe(observation(seq, seq*.1, size=.1))
        self.assertTrue(c.resume(.71)[0])
        self.assertEqual(c.reference_size, .2)
        self.assertIsNone(c.status()["openpose_control"]["last_dt"])
        c.pause()
        c.observe(observation(8, .8, size=.15))
        self.assertTrue(c.calibrate(.8)[0])
        self.assertEqual(c.reference_size, .15)
        self.assertEqual(c.status()["openpose_control"]["pitch_components"], (0, 0, 0))

    def test_rate_limit_direction_reversal_does_not_drag_old_motion(self):
        c = self.ready(max_request_rate=.8)
        for seq in range(1, 8):
            self.sample(c, seq, seq*.1, x=.8, size=.1)
        self.zero(self.sample(c, 8, .71, x=.2, size=.4))
        r = self.sample(c, 9, .8, x=.2, size=.4)
        self.assertLess(r.yaw, 0)
        self.assertLess(r.forward, 0)
        self.assertLessEqual(abs(r.forward), .8*.09+1e-12)

    def test_recovery_does_not_reuse_old_pid_or_restart_after_explicit_stop(self):
        for cancel in (False, True):
            c = self.ready()
            self.sample(c, 1, .1, x=.8, size=.1)
            self.zero(self.sample(c, 2, .2, found=False))
            if cancel:
                c.stop()
            for seq in range(3, 7):
                request = self.sample(c, seq, seq*.1, x=.8, size=.1)
            if cancel:
                self.zero(request)
                self.assertEqual(c.state, State.STOPPED)
            else:
                self.assertEqual(c.state, State.TRACKING)
                self.assertGreater(request.forward, 0)
                self.assertGreater(request.yaw, 0)

    def test_profile_configuration_is_explicit_and_default_is_formal_pid(self):
        self.assertEqual(FollowController(config=FollowConfig(max_request_rate=None)).control_profile, "openpose_pid")
        for profile in ("pid", "", None, False):
            with self.assertRaises(ValueError):
                FollowController(config=FollowConfig(control_profile=profile, max_request_rate=None))
        self.assertEqual(FollowController(config=FollowConfig(control_profile="openpose_pid", max_request_rate=None)).steering_mode, "yaw")
        self.assertEqual(FollowController(config=FollowConfig(control_profile="openpose_pid", steering_mode="lateral", max_request_rate=None)).steering_mode, "lateral")
        self.assertEqual(FollowController(config=FollowConfig(steering_mode="hybrid")).steering_mode, "hybrid")
        self.assertFalse(self.ready().status()["openpose_control"]["physical_tuning_verified"])

    def test_malformed_observation_values_fail_closed_before_pid_call(self):
        for field, value in (("center_x", True), ("center_y", False),
                             ("size_ratio", True), ("acquired_at", True),
                             ("center_x", float("nan")), ("size_ratio", float("inf")),
                             ("target_id", False), ("target_id", 0.)):
            with self.subTest(field=field, value=value):
                c = self.ready()
                self.assertFalse(c.observe(replace(observation(1, .1), **{field: value})))
                self.zero(c.tick(.1))
                self.assertEqual(c.state, State.INVALID)




if __name__ == "__main__":
    unittest.main()
