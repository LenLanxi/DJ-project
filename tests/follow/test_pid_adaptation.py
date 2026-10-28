from follow.r3_follow.config import FollowConfig
"""Marker measurement adaptation acceptance checks; no flight claims/devices."""
import math
import unittest

from follow.r3_follow.openpose_control import OpenPoseMarkerControl


class MarkerPidAdaptationTests(unittest.TestCase):
    def call(self, control, seq, t, size=.2, **kwargs):
        return control.update(frame_id=seq, acquired_at=t, center_x=360., image_width=640,
                              size_ratio=size, reference_size=.2, **kwargs)

    def test_formal_parameter_sets_are_explicit(self):
        current = OpenPoseMarkerControl(FollowConfig())
        original = OpenPoseMarkerControl(FollowConfig(pid_tuning="responsive_v1"))
        self.assertEqual(current.pitch_pid.tunings, (.5, .01, .06))
        self.assertGreater(original.pitch_pid.Kp, current.pitch_pid.Kp)
        self.assertGreater(current.pitch_pid.Ki, 0)
        self.assertGreater(current.pitch_pid.Kd, 0)
        self.assertEqual(current.yaw_pid.tunings, original.yaw_pid.tunings)
        for bad in ("", "auto", False, None):
            with self.assertRaises(ValueError):
                OpenPoseMarkerControl(FollowConfig(pid_tuning=bad))

    def test_filter_uses_elapsed_time_not_frame_count(self):
        def filtered(times):
            control = OpenPoseMarkerControl(FollowConfig())
            self.call(control, 0, 0, .2)
            for seq, t in enumerate(times, 1):
                self.call(control, seq, t, .15)
            return control.status()["filtered_size_delta_equivalent_px"]
        expected = (.15-.2)*640*(1-math.exp(-.2/.08))
        for times in ((.02, .04, .06, .08, .1, .12, .14, .16, .18, .2),
                      (.1, .2), (.011, .031, .126, .2)):
            self.assertAlmostEqual(filtered(times), expected, places=10)

    def test_fresh_frame_changes_pid_without_extra_sample_hold(self):
        control = OpenPoseMarkerControl(FollowConfig())
        self.call(control, 0, 0, .2)
        output = self.call(control, 1, .01, .15)
        self.assertGreater(output[1], 0)
        self.assertEqual(control.status()["last_pitch_dt"], .01)
        self.assertEqual(control.status()["depth_update_reason"], "new_measurement")

    def test_duplicate_reads_do_not_age_filter_or_integral(self):
        control = OpenPoseMarkerControl(FollowConfig())
        self.call(control, 0, 0, .2)
        expected = self.call(control, 1, .1, .15)
        before = control.status()
        for t in (.11, .2, .3, .4):
            self.assertEqual(self.call(control, 1, t, .12), expected)
            after = control.status()
            self.assertEqual(after["filtered_size_delta_equivalent_px"], before["filtered_size_delta_equivalent_px"])
            self.assertEqual(after["pitch_components"], before["pitch_components"])

    def test_filter_and_all_pid_history_clear_on_reset_gap_and_shape_block(self):
        for reset in ("explicit", "gap", "shape"):
            with self.subTest(reset=reset):
                control = OpenPoseMarkerControl(FollowConfig())
                for seq in range(20):
                    self.call(control, seq, seq*.05, .12)
                if reset == "explicit":
                    control.reset()
                elif reset == "shape":
                    blocked = self.call(control, 20, 1., .12, forward_allowed=False)
                    self.assertEqual(blocked[1], 0)
                    self.assertIsNone(control.status()["filtered_size_delta_equivalent_px"])
                next_at = 2. if reset == "gap" else 1.1
                fresh = OpenPoseMarkerControl(FollowConfig())
                self.assertEqual(self.call(control, 21, next_at, .23), self.call(fresh, 21, next_at, .23))
                self.assertEqual(control.pitch_pid.components, fresh.pitch_pid.components)





    def test_same_direction_saturation_does_not_accumulate_integral_for_a_minute(self):
        for size, sign in ((.1, 1), (.3, -1)):
            control = OpenPoseMarkerControl(FollowConfig())
            for seq in range(601):
                result = self.call(control, seq, seq*.1, size)
                self.assertEqual(result[1], sign*.3)
            self.assertLess(abs(control.pitch_pid.components[1]), 1e-12)
            self.assertTrue(control.status()["depth_integral_paused"])
            self.assertEqual(control.pitch_pid.Ki, .01)

    def test_opposing_error_can_unwind_after_saturation(self):
        control = OpenPoseMarkerControl(FollowConfig())
        for seq in range(31):
            self.call(control, seq, seq*.1, .1)
        self.call(control, 31, 3.1, .35)
        self.assertFalse(control.status()["depth_integral_paused"])
        self.assertLess(control.pitch_pid.components[1], 0)
        self.assertEqual(control.pitch_pid.Ki, .01)

    def test_reference_band_clears_preexisting_unsaturated_integral_immediately(self):
        for size in (.16, .24):
            control = OpenPoseMarkerControl(FollowConfig())
            for seq in range(301):
                self.call(control, seq, seq*.1, size)
            self.assertGreater(abs(control.pitch_pid.components[1]), 1)
            for seq in range(301, 501):
                result = self.call(control, seq, seq*.1, .2+(.0008 if seq % 2 else -.0008))
                self.assertEqual(result[1], 0)
                self.assertEqual(control.pitch_pid.components, (0, 0, 0))
                self.assertEqual(control.status()["depth_update_reason"], "at_reference_zero_depth")
            self.assertEqual(control.yaw_pid.components[1], 0)
            self.assertGreater(self.call(control, 501, 50.1, .19)[1], 0)

    def test_direct_reference_change_same_frame_is_bumpless_and_requires_new_frame(self):
        control = OpenPoseMarkerControl(FollowConfig())
        for seq in range(11):
            self.call(control, seq, seq*.1, .12)
        def changed(seq, t, size):
            return control.update(frame_id=seq, acquired_at=t, center_x=360, image_width=640,
                                  size_ratio=size, reference_size=.12)
        self.assertEqual(changed(10, 1., .12), (0, 0, 0))
        self.assertEqual(control.reason, "reference_changed_wait_new_frame")
        self.assertEqual(control.pitch_pid.components, (0, 0, 0))
        self.assertEqual(changed(10, 1., .1), (0, 0, 0))
        result = changed(11, 1.1, .1)
        self.assertGreater(result[1], 0)
        self.assertLess(result[1], .1)
        self.assertEqual(control.pitch_pid.components[2], 0)

    def test_older_frame_cannot_change_reference_or_clear_current_motion(self):
        control = OpenPoseMarkerControl(FollowConfig())
        expected = self.call(control, 10, 1., .12)
        got = control.update(frame_id=9, acquired_at=.9, center_x=360, image_width=640,
                             size_ratio=.12, reference_size=.12)
        self.assertEqual(got, expected)
        self.assertEqual(control.status()["reference_size"], .2)


if __name__ == "__main__":
    unittest.main()
