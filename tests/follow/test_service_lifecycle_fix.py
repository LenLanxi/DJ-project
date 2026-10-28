from follow.r3_follow.config import load_config
"""Default yaw dispatch and lifecycle regressions, no SDK/network/hardware."""
import unittest
from unittest.mock import patch

from follow.r3_follow.core import Observation, State, MotionRequest
from tests.helpers.FollowFixture import FakeDevice
from follow.r3_follow import FollowService
from drone_sdk.contract import CommandResult


from FollowFixtures import GeometryDetector


class ServiceFixTests(unittest.TestCase):
    def setUp(self):
        self.t, self.seq = 10., 0
        self.port = FakeDevice(lambda: self.t)
        self.service = FollowService(self.port, tracker=GeometryDetector(),
            monotonic=lambda: self.t, wall_clock=lambda: 1800000000+self.t,
            recovery_window=3, log_directory=False, config=load_config())
        self.addCleanup(self.service.close)
        self.frame()
        self.assertTrue(self.service.calibrate_reference().ok)
        self.assertTrue(self.service._start_tracking().ok)

    def frame(self, x=320, size=160, found=True, advance=.1):
        self.t += advance
        self.seq += 1
        self.port.publish((found, x, size), self.seq, 1800000000+self.t)
        self.assertTrue(self.service.capture_once())
        self.service.control_once()

    def test_default_yaw_tracks_visible_target_while_adjusting_depth(self):
        for _ in range(6):
            self.frame(x=550, size=80)
        self.assertEqual(self.port.velocity[:3], (0, 30, 0))
        self.assertEqual(self.port.velocity[3], 20)
        self.assertEqual(self.service.status()["steering"]["mode"], "yaw")
        for _ in range(5):
            self.frame(size=80)
        self.assertEqual(self.port.velocity, (0, 30, 0, 0))

    def test_yaw_reversal_clears_old_direction_before_new_turn(self):
        self.frame(x=550)
        self.assertGreater(self.port.velocity[3], 0)
        self.frame(x=90, advance=.01)
        self.assertEqual(self.port.velocity, (0,0,0,0))
        self.frame(x=90)
        self.assertLess(self.port.velocity[3], 0)

    def test_new_yaw_waits_for_send_deadline_without_interrupting_valid_depth(self):
        for _ in range(5):
            self.frame(size=80)
        self.assertEqual(self.port.velocity[1], 30)
        sent_at = self.service.last_send
        self.frame(x=550, size=80, advance=.01)
        self.assertEqual(self.port.velocity, (0,30,0,0))
        self.assertEqual(self.service.last_send, sent_at)
        self.frame(x=550, size=80)
        self.assertGreater(self.port.velocity[3], 0)
        self.assertEqual(self.port.velocity[1], 30)

    def test_repeat_start_during_recovery_keeps_original_deadline_and_reference(self):
        self.frame(x=550)
        self.frame(found=False)
        lease, ref, lost = self.service.grant, self.service.controller.reference_size, self.t
        for _ in range(14):
            self.frame(found=False, advance=.2)
            result = self.service._start_tracking()
            self.assertTrue(result.ok)
            self.assertEqual(result.reason, "waiting_for_target")
            self.assertEqual(self.service.grant, lease)
            self.assertEqual(self.service.controller.reference_size, ref)
            self.assertEqual(self.port.velocity, (0,0,0,0))
        self.frame(found=False, advance=.21)
        self.assertGreater(self.t-lost, 3)
        self.assertEqual(self.service.controller.state, State.LOST)
        self.assertIsNone(self.service.grant)
        self.assertFalse(self.service._start_tracking().ok)

    def test_recovery_is_active_but_is_following_stays_false(self):
        self.frame(found=False)
        status = self.service.status()
        self.assertTrue(status["active"])
        self.assertTrue(status["recovering"])
        self.assertFalse(status["following"])
        self.assertFalse(status["cleanup_pending"])

    def test_stop_during_recovery_prevents_target_return_from_restarting(self):
        self.frame(found=False)
        self.service._stop_tracking()
        for _ in range(5):
            self.frame(x=550)
        self.assertEqual(self.port.velocity, (0,0,0,0))
        self.assertFalse(self.service.status()["active"])
        self.assertIsNone(self.port.lease)

    def test_failed_release_retains_pending_lease_and_retries_without_motion(self):
        self.frame(x=550)
        def zero_then_fail(owner, **grant):
            self.assertTrue(self.port.stop_motion(owner, control_epoch=grant['control_epoch']).ok)
            return CommandResult(False, 'failed', 'injected_release_after_zero')
        with patch.object(self.port, "release_control", side_effect=zero_then_fail) as release:
            self.assertFalse(self.service._stop_tracking().ok)
            status = self.service.status()
            self.assertTrue(status["cleanup_pending"])
            self.assertTrue(status["cleanup_unconfirmed"])
            self.assertEqual(release.call_count, 1)
            self.t += .11
            self.service.control_once()
            self.assertEqual(release.call_count, 2)
            self.assertEqual(self.port.velocity, (0,0,0,0))
        self.t += .11
        self.service.control_once()
        self.assertIsNone(self.service.grant)
        self.assertFalse(self.service.status()["cleanup_pending"])
        self.assertFalse(self.service.status()["cleanup_unconfirmed"])

    def test_pending_cleanup_does_not_stop_new_owner(self):
        with patch.object(self.port, "release_control", return_value=CommandResult(False, "failed", "injected")):
            self.service._stop_tracking()
        other = self.port.revoke_for_test("model")
        self.port.update_velocity(other, left_right=0, forward_back=11, up_down=0, yaw=12)
        self.t += .11
        self.service.control_once()
        self.assertEqual(self.port.velocity, (0,11,0,12))
        self.assertIsNone(self.service.grant)

    def test_all_loss_paths_clear_yaw(self):
        for case in ("loss", "stale", "rejected", "closed"):
            with self.subTest(case=case):
                # New instance per path, sharing neither lease nor frame state.
                self.service.close()
                self.setUp()
                self.frame(x=550)
                self.assertGreater(self.port.velocity[3], 0)
                if case == "loss": self.frame(found=False, advance=.01)
                elif case == "stale":
                    self.t += 1.01
                    self.service.control_once()
                elif case == "rejected":
                    with patch.object(self.port, "update_velocity", return_value=CommandResult(False, "failed", "injected")):
                        self.frame(x=550)
                else: self.service.close()
                self.assertEqual(self.port.velocity, (0,0,0,0))

    def test_handoff_preserves_reference_and_rejects_late_completion_after_stop(self):
        reference = self.service.controller.reference_size
        ticket = self.service.pause_if_active()
        self.assertIsNotNone(ticket)
        self.frame(size=80)
        self.assertTrue(self.service.finish_action(ticket, True).ok)
        self.assertEqual(reference, self.service.controller.reference_size)
        ticket = self.service.pause_if_active()
        self.service._stop_tracking()
        self.frame(x=550)
        self.assertFalse(self.service.finish_action(ticket, True).ok)
        self.assertEqual(self.port.velocity, (0,0,0,0))



if __name__ == "__main__":
    unittest.main()
