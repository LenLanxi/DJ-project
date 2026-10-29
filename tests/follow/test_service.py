from follow.r3_follow.config import FollowConfig
import threading
import time
import unittest
from unittest.mock import patch

from follow.r3_follow.core import Observation
from tests.helpers.FollowFixture import FakeDevice
from follow.r3_follow import FollowService
from drone_sdk.contract import CommandResult


from FollowFixtures import Clock


from FollowFixtures import Detector


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.port = FakeDevice(self.clock.mono)
        self.service = FollowService(self.port, config=FollowConfig(slew_rc_per_second=80.0),
                           tracker=Detector(),
                           monotonic=self.clock.mono,
                           wall_clock=self.clock.wall,
                           log_directory=False)
        # Exercise current lateral control; yaw has a separate service suite.
        self.seq = 0
        self.addCleanup(self.service.close)
        self.frame()
        self.assertTrue(self.service.calibrate_reference().ok)

    def frame(self, x=320, found=True, advance=.1):
        self.clock.now += advance
        self.seq += 1
        self.port.publish((found, x), self.seq, self.clock.wall())
        self.service.capture_once()

    def moving(self):
        self.assertTrue(self.service._start_tracking().ok)
        self.frame(550)
        self.service.control_once()
        self.assertGreater(self.port.velocity[0] + self.port.velocity[3], 0)

    def test_start_needs_calibration_and_target(self):
        self.service.controller.reference_size = None
        self.assertFalse(self.service._start_tracking().ok)
        self.assertIsNone(self.port.lease)

    def test_lifecycle(self):
        self.moving()
        token = self.service.pause_if_active()
        self.assertIsNotNone(token)
        self.assertEqual(self.port.velocity, (0, 0, 0, 0))
        self.assertFalse(self.service.is_following())
        self.frame(550)
        self.assertTrue(self.service.finish_action(token, True).ok)
        self.assertTrue(self.service.stop().ok)
        self.frame(550)
        self.service.control_once()
        self.assertFalse(self.service.is_following())
        self.assertIsNone(self.service.pause_if_active())

    def test_busy_does_not_change_other_owner(self):
        other = (self.port.request_control("model"), self.port.lease)[1]
        self.port.update_velocity(other, left_right=7, forward_back=0, up_down=0, yaw=0)
        self.assertEqual(self.service._start_tracking().reason, "busy")
        self.service._stop_tracking()
        self.assertEqual(self.port.velocity, (7, 0, 0, 0))

    def test_duplicate_frame_never_refreshes_age(self):
        self.moving()
        self.clock.now += 1.1
        self.assertFalse(self.service.capture_once())
        self.service.control_once()
        self.assertEqual(self.port.velocity, (0, 0, 0, 0))
        self.assertEqual(self.service.status()["state"], "recovering_target")

    def test_changed_duplicate_timestamp_is_rejected(self):
        self.moving()
        self.clock.now += .1
        self.port.publish((True, 550), self.seq, self.clock.wall())
        self.assertFalse(self.service.capture_once())
        self.assertEqual(self.port.velocity, (0, 0, 0, 0))
        self.assertIn("cached_frame_timestamp_changed", self.service.last_reason)

    def test_frame_id_reset_is_rejected(self):
        self.moving()
        self.port.publish((True, 550), 0, self.clock.wall())
        self.assertFalse(self.service.capture_once())
        self.assertFalse(self.service.is_following())

    def test_missing_frame_stops_without_waiting_one_second(self):
        self.moving()
        self.port.publish(None, self.seq, self.port.snapshot.timestamp, stale=True)
        self.service.capture_once()
        self.assertEqual(self.port.velocity, (0, 0, 0, 0))

    def test_missing_video_recovers_with_original_reference_but_explicit_stop_does_not(self):
        self.moving()
        reference = self.service.controller.reference_size
        self.port.publish(None, self.seq, self.port.snapshot.timestamp, stale=True)
        self.service.capture_once()
        self.assertEqual(self.port.velocity, (0, 0, 0, 0))
        self.clock.now += 2
        for _ in range(4):
            self.frame(550)
            self.service.control_once()
        self.assertTrue(self.service.is_following())
        self.assertEqual(self.service.controller.reference_size, reference)
        self.service.stop()
        for _ in range(4):
            self.frame(550)
            self.service.control_once()
        self.assertFalse(self.service.is_following())
        self.assertEqual(self.port.velocity, (0, 0, 0, 0))

    def test_missing_frame_blocks_restart_even_when_previous_detection_was_recent(self):
        self.moving()
        self.port.publish(None, self.seq + 1, self.clock.wall(), stale=False)
        self.assertFalse(self.service.capture_once())
        self.assertEqual(self.service._start_tracking().reason, "waiting_for_target")
        self.assertFalse(self.service.calibrate_reference().ok)
        self.frame(550)
        self.assertTrue(self.service._start_tracking().ok)

    def test_bad_cached_metadata_blocks_restart_until_next_valid_frame(self):
        self.moving()
        self.port.publish((True, 550), self.seq, self.clock.wall() + .01)
        self.assertFalse(self.service.capture_once())
        self.assertEqual(self.service._start_tracking().reason, "waiting_for_target")
        self.frame(550)
        self.assertTrue(self.service._start_tracking().ok)

    def test_expired_new_frame_is_rejected(self):
        self.moving()
        self.port.publish((True, 550), self.seq + 1, self.clock.wall() - 1.1)
        self.assertFalse(self.service.capture_once())
        self.assertFalse(self.service.is_following())

    def test_future_timestamp_is_rejected(self):
        self.moving()
        self.port.publish((True, 550), self.seq + 1, self.clock.wall() + 1)
        self.assertFalse(self.service.capture_once())
        self.assertFalse(self.service.is_following())

    def test_loss_and_reappearance_do_not_restart(self):
        self.moving()
        self.frame(found=False)
        self.service.control_once()
        self.assertEqual(self.port.velocity, (0, 0, 0, 0))
        self.frame(550)
        self.assertFalse(self.service.is_following())
        self.assertTrue(self.service._start_tracking().ok)

    def test_no_velocity_queue_and_rate_limited(self):
        self.moving()
        before = len([e for e in self.port.events if e["event"] == "velocity"])
        for _ in range(10):
            self.service.control_once()
        self.assertEqual(before, len([e for e in self.port.events if e["event"] == "velocity"]))
        self.frame(50)
        self.service.control_once()
        self.assertEqual(self.port.velocity, (0, 0, 0, 0))  # brake first
        self.frame(50)
        self.service.control_once()
        self.assertLess(self.port.velocity[0] + self.port.velocity[3], 0)
        self.assertEqual(self.port.velocity[2], 0)

    def test_ownership_revocation_does_not_stop_new_owner(self):
        self.moving()
        old = self.service.grant
        new = self.port.revoke_for_test()
        self.port.update_velocity(new, left_right=-5, forward_back=0, up_down=0, yaw=0)
        self.service.control_once()
        self.assertFalse(self.service.is_following())
        self.assertEqual(self.port.velocity, (-5, 0, 0, 0))
        self.assertFalse(self.port.update_velocity(old, left_right=25, forward_back=0, up_down=0, yaw=0).ok)

    def test_same_owner_new_lease_rejects_old_request(self):
        self.moving()
        old = self.service.grant
        self.port.revoke_for_test("follow")
        self.assertFalse(self.port.update_velocity(old, left_right=25, forward_back=0, up_down=0, yaw=0).ok)
        self.service.control_once()
        self.assertFalse(self.service.is_following())

    def test_handoff_accepted_waits_completed_resumes(self):
        self.moving()
        ticket = self.service.pause_if_active()
        self.assertIsNotNone(ticket)
        self.assertIsNone(self.port.lease)
        other = (self.port.request_control("model"), self.port.lease)[1]
        self.frame(50)
        self.assertFalse(self.service.is_following())
        self.port.release_control(other)
        self.assertTrue(self.service.finish_action(ticket, True).ok)
        self.assertEqual(self.port.velocity, (0, 0, 0, 0))  # gradual restart
        self.frame(50)
        self.service.control_once()
        self.assertLess(self.port.velocity[0] + self.port.velocity[3], 0)
        self.assertFalse(self.service.finish_action(ticket, True).ok)

    def test_stop_during_handoff_cancels_late_completion(self):
        self.moving()
        ticket = self.service.pause_if_active()
        self.service._stop_tracking()
        self.frame(550)
        self.assertFalse(self.service.finish_action(ticket, True).ok)
        self.assertFalse(self.service.is_following())

    def test_failed_action_does_not_resume(self):
        self.moving()
        ticket = self.service.pause_if_active()
        self.frame(550)
        self.assertFalse(self.service.finish_action(ticket, False).ok)
        self.assertFalse(self.service.is_following())

    def test_handoff_requires_new_frame(self):
        self.moving()
        ticket = self.service.pause_if_active()
        self.assertFalse(self.service.finish_action(ticket, True).ok)

    def test_handoff_does_not_start_previously_stopped_service(self):
        ticket = self.service.pause_if_active()
        self.frame(550)
        self.assertFalse(self.service.finish_action(ticket, True).ok)

    def test_handoff_busy_requires_explicit_retry(self):
        self.moving()
        ticket = self.service.pause_if_active()
        lease = (self.port.request_control("model"), self.port.lease)[1]
        self.frame(550)
        self.assertEqual(self.service.finish_action(ticket, True).reason, "busy")
        self.port.release_control(lease)
        self.assertFalse(self.service.is_following())

    def test_disconnection_stops(self):
        self.moving()
        self.port.connected = False
        self.service.control_once()
        self.assertEqual(self.port.velocity, (0, 0, 0, 0))
        self.assertFalse(self.service.is_following())

    def test_unknown_flight_state_prevents_start(self):
        self.port.airborne = None
        self.assertFalse(self.service._start_tracking().ok)

    def test_rejected_velocity_fails_closed(self):
        with patch.object(self.port, "update_velocity", return_value=CommandResult(False, "failed", "injected")):
            self.assertFalse(self.service._start_tracking().ok)
        self.assertIsNone(self.port.lease)

    def test_cleanup_error_is_reported(self):
        self.moving()
        with patch.object(self.port, "release_control", side_effect=RuntimeError("injected")):
            result = self.service._stop_tracking()
        self.assertFalse(result.ok)
        self.assertIsNotNone(self.service.grant)
        self.assertFalse(self.service._start_tracking().ok)
        self.assertTrue(self.service._stop_tracking().ok)

    def test_backend_watchdog_test_double(self):
        self.moving()
        self.clock.now += .51
        self.port.watchdog_step()
        self.assertEqual(self.port.velocity, (0, 0, 0, 0))

    def test_closed_service_cannot_restart(self):
        self.moving()
        self.service.close()
        self.assertFalse(self.service._start_tracking().ok)
        self.assertFalse(self.service.capture_once())
        with self.assertRaises(RuntimeError):
            self.service.start_background()

    def test_device_classification_is_captured_from_the_bound_device(self):
        self.assertTrue(self.service.status()['simulated'])
        device = FakeDevice()
        device.simulated = False
        service = FollowService(device, config=self.service.config, log_directory=False)
        self.addCleanup(service.close)
        self.assertFalse(service.status()['simulated'])
        service._record('classification')
        self.assertFalse(service.events()[-1]['simulated'])

    def test_network_is_not_used(self):
        with patch("socket.socket", side_effect=AssertionError("network forbidden")):
            self.moving()
            self.service._stop_tracking()

    def test_optional_brief_loss_holds_zero_until_stable_recovery(self):
        self.service.controller.auto_recover = True
        self.moving()
        self.frame(found=False, advance=.02)
        self.service.control_once()
        self.assertEqual(self.port.velocity, (0, 0, 0, 0))
        self.assertEqual(self.service.status()["state"], "recovering_target")
        for _ in range(2):
            self.frame(550)
            self.service.control_once()
            self.assertEqual(self.port.velocity, (0, 0, 0, 0))
        self.frame(550)
        self.service.control_once()
        self.assertTrue(self.service.is_following())
        self.assertEqual(self.port.velocity, (0, 0, 0, 0))
        self.frame(550)
        self.service.control_once()
        self.assertGreater(self.port.velocity[0] + self.port.velocity[3], 0)

    def test_expired_follow_before_handoff_can_resume_on_new_frame(self):
        self.moving()
        self.clock.now += 1.1
        ticket = self.service.pause_if_active()
        self.frame(550)
        self.assertTrue(self.service.finish_action(ticket, True).ok)

    def test_detection_completion_does_not_make_old_capture_fresh(self):
        self.moving()
        detect = self.service.tracker.detect
        def slow(*args, **kwargs):
            self.clock.now += 1.1
            return detect(*args, **kwargs)
        with patch.object(self.service.tracker, "detect", side_effect=slow):
            self.frame(550)
        self.service.control_once()
        self.assertEqual(self.port.velocity, (0, 0, 0, 0))
        self.assertFalse(self.service.is_following())


class WorkerTests(unittest.TestCase):
    def test_blocked_vision_does_not_block_expiry_or_shutdown(self):
        entered, unblock = threading.Event(), threading.Event()
        class BlockingDetector(Detector):
            def detect(self, *args, **kwargs):
                entered.set()
                unblock.wait(3)
                return super().detect(*args, **kwargs)
        port = FakeDevice()
        service = FollowService(port, config=FollowConfig(slew_rc_per_second=80.0),
                      tracker=Detector(),
                      log_directory=False)
        try:
            port.publish((True, 320), 1, time.time())
            service.capture_once()
            self.assertTrue(service.calibrate_reference().ok)
            self.assertTrue(service._start_tracking().ok)
            # Distinct captures need distinct timestamps. Windows time.time()
            # may return the same value for two immediate synthetic publishes.
            time.sleep(.02)
            port.publish((True, 550), 2, time.time())
            service.capture_once()
            time.sleep(.11)
            service.control_once()
            self.assertGreater(port.velocity[0] + port.velocity[3], 0)
            service.tracker = BlockingDetector()
            port.publish((True, 550), 3, time.time())
            service.start_background()
            self.assertTrue(entered.wait(1))
            deadline = time.monotonic() + 1.5
            while port.velocity != (0, 0, 0, 0) and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(port.velocity, (0, 0, 0, 0))
            self.assertEqual(service.status()["state"], "recovering_target")
            service.close()
            unblock.set()
            for worker in service.workers:
                worker.join(1)
            self.assertFalse(service.is_following())
            self.assertFalse(any(t.is_alive() for t in service.workers))
        finally:
            unblock.set()
            service.close()


if __name__ == "__main__":
    unittest.main()
