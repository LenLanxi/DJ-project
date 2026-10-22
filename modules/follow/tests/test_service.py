import threading
import time
import unittest
from unittest.mock import patch

from r3_follow.core import Observation
from r3_follow.mock_port import MockPort
from r3_follow.service import FollowService
from r3_follow.service_ports import Result


class Clock:
    def __init__(self):
        self.now = 10.0
    def mono(self):
        return self.now
    def wall(self):
        return 1800000000.0 + self.now


class Detector:
    def detect(self, frame, frame_id, acquired_at, source):
        corners = ((frame[1]-80,160), (frame[1]+80,160),
                   (frame[1]+80,320), (frame[1]-80,320)) if frame[0] else ()
        return Observation(frame_id, acquired_at, source, 640, 480, 0,
                           frame[0], frame[1], 240, .25, corners=corners,
                           reason="not_found" if not frame[0] else "")


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.port = MockPort(self.clock.mono)
        self.service = FollowService(self.port, tracker=Detector(),
            monotonic=self.clock.mono, wall_clock=self.clock.wall)
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
        self.assertTrue(self.service.start_follow().ok)
        self.frame(550)
        self.service.control_once()
        self.assertGreater(self.port.velocity[0], 0)

    def test_start_needs_calibration_and_target(self):
        self.service.controller.reference_size = None
        self.assertFalse(self.service.start_follow().ok)
        self.assertIsNone(self.port.lease)

    def test_lifecycle(self):
        self.moving()
        self.assertTrue(self.service.pause_follow().ok)
        self.assertEqual(self.port.velocity, (0, 0, 0, 0))
        self.assertIsNone(self.port.lease)
        self.assertFalse(self.service.is_following())
        self.assertTrue(self.service.resume_follow().ok)
        self.assertTrue(self.service.stop_follow().ok)
        self.frame(550)
        self.service.control_once()
        self.assertFalse(self.service.is_following())
        self.assertFalse(self.service.resume_follow().ok)

    def test_busy_does_not_change_other_owner(self):
        other = self.port.request_control("model").lease
        self.port.update_velocity(other, left_right=7, forward_back=0, up_down=0, yaw=0)
        self.assertEqual(self.service.start_follow().reason, "busy")
        self.service.stop_follow()
        self.assertEqual(self.port.velocity, (7, 0, 0, 0))

    def test_duplicate_frame_never_refreshes_age(self):
        self.moving()
        self.clock.now += .6
        self.assertFalse(self.service.capture_once())
        self.service.control_once()
        self.assertEqual(self.port.velocity, (0, 0, 0, 0))
        self.assertEqual(self.service.get_follow_status()["state"], "frame_stale")

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

    def test_missing_frame_blocks_restart_even_when_previous_detection_was_recent(self):
        self.moving()
        self.port.publish(None, self.seq + 1, self.clock.wall(), stale=False)
        self.assertFalse(self.service.capture_once())
        self.assertEqual(self.service.start_follow().reason, "new_valid_frame_required")
        self.assertFalse(self.service.calibrate_reference().ok)
        self.frame(550)
        self.assertTrue(self.service.start_follow().ok)

    def test_bad_cached_metadata_blocks_restart_until_next_valid_frame(self):
        self.moving()
        self.port.publish((True, 550), self.seq, self.clock.wall() + .01)
        self.assertFalse(self.service.capture_once())
        self.assertFalse(self.service.start_follow().ok)
        self.frame(550)
        self.assertTrue(self.service.start_follow().ok)

    def test_expired_new_frame_is_rejected(self):
        self.moving()
        self.port.publish((True, 550), self.seq + 1, self.clock.wall() - .6)
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
        self.assertTrue(self.service.start_follow().ok)

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
        self.assertLess(self.port.velocity[0], 0)
        self.assertEqual(self.port.velocity[2:], (0, 0))

    def test_ownership_revocation_does_not_stop_new_owner(self):
        self.moving()
        old = self.service.lease
        new = self.port.revoke_for_test()
        self.port.update_velocity(new, left_right=-5, forward_back=0, up_down=0, yaw=0)
        self.service.control_once()
        self.assertFalse(self.service.is_following())
        self.assertEqual(self.port.velocity, (-5, 0, 0, 0))
        self.assertFalse(self.port.update_velocity(old, left_right=25, forward_back=0, up_down=0, yaw=0).ok)

    def test_same_owner_new_lease_rejects_old_request(self):
        self.moving()
        old = self.service.lease
        self.port.revoke_for_test("follow")
        self.assertFalse(self.port.update_velocity(old, left_right=25, forward_back=0, up_down=0, yaw=0).ok)
        self.service.control_once()
        self.assertFalse(self.service.is_following())

    def test_handoff_accepted_waits_completed_resumes(self):
        self.moving()
        result, ticket = self.service.pause_for_action()
        self.assertTrue(result.ok)
        self.assertIsNone(self.port.lease)
        other = self.port.request_control("model").lease
        self.frame(50)
        self.assertEqual(self.service.finish_action(ticket, "accepted").status, "accepted")
        self.assertFalse(self.service.is_following())
        self.port.release_control(other)
        self.assertTrue(self.service.finish_action(ticket, "completed").ok)
        self.assertEqual(self.port.velocity, (0, 0, 0, 0))  # gradual restart
        self.frame(50)
        self.service.control_once()
        self.assertLess(self.port.velocity[0], 0)
        self.assertFalse(self.service.finish_action(ticket, "completed").ok)

    def test_stop_during_handoff_cancels_late_completion(self):
        self.moving()
        _, ticket = self.service.pause_for_action()
        self.service.stop_follow()
        self.frame(550)
        self.assertFalse(self.service.finish_action(ticket, "completed").ok)
        self.assertFalse(self.service.is_following())

    def test_failed_action_does_not_resume(self):
        self.moving()
        _, ticket = self.service.pause_for_action()
        self.frame(550)
        self.assertFalse(self.service.finish_action(ticket, "timeout").ok)
        self.assertFalse(self.service.is_following())

    def test_handoff_requires_new_frame(self):
        self.moving()
        _, ticket = self.service.pause_for_action()
        self.assertFalse(self.service.finish_action(ticket, "completed").ok)

    def test_handoff_does_not_start_previously_stopped_service(self):
        _, ticket = self.service.pause_for_action()
        self.frame(550)
        self.assertFalse(self.service.finish_action(ticket, "completed").ok)

    def test_handoff_busy_requires_explicit_retry(self):
        self.moving()
        _, ticket = self.service.pause_for_action()
        lease = self.port.request_control("model").lease
        self.frame(550)
        self.assertEqual(self.service.finish_action(ticket, "completed").reason, "busy")
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
        self.assertFalse(self.service.start_follow().ok)

    def test_rejected_velocity_fails_closed(self):
        with patch.object(self.port, "update_velocity", return_value=Result("failed", "injected")):
            self.assertFalse(self.service.start_follow().ok)
        self.assertIsNone(self.port.lease)

    def test_cleanup_error_is_reported(self):
        self.moving()
        with patch.object(self.port, "stop_motion", side_effect=RuntimeError("injected")):
            result = self.service.stop_follow()
        self.assertFalse(result.ok)
        self.assertIsNotNone(self.service.lease)
        self.assertFalse(self.service.start_follow().ok)
        self.assertTrue(self.service.stop_follow().ok)

    def test_backend_watchdog_test_double(self):
        self.moving()
        self.clock.now += .51
        self.port.watchdog_step()
        self.assertEqual(self.port.velocity, (0, 0, 0, 0))

    def test_closed_service_cannot_restart(self):
        self.moving()
        self.service.close()
        self.assertFalse(self.service.start_follow().ok)
        self.assertFalse(self.service.capture_once())
        with self.assertRaises(RuntimeError):
            self.service.start_background()

    def test_real_backend_guard(self):
        self.port.simulation_only = False
        with self.assertRaises(ValueError):
            FollowService(self.port)

    def test_network_is_not_used(self):
        with patch("socket.socket", side_effect=AssertionError("network forbidden")):
            self.moving()
            self.service.stop_follow()

    def test_optional_brief_loss_holds_zero_until_stable_recovery(self):
        self.service.controller.recovery_window = 1.5
        self.moving()
        self.frame(found=False, advance=.02)
        self.service.control_once()
        self.assertEqual(self.port.velocity, (0, 0, 0, 0))
        self.assertEqual(self.service.get_follow_status()["state"], "recovering_target")
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
        self.assertGreater(self.port.velocity[0], 0)

    def test_expired_follow_before_handoff_cannot_resume(self):
        self.moving()
        self.clock.now += .6
        _, ticket = self.service.pause_for_action()
        self.frame(550)
        self.assertFalse(self.service.finish_action(ticket, "completed").ok)

    def test_detection_completion_does_not_make_old_capture_fresh(self):
        self.moving()
        detect = self.service.tracker.detect
        def slow(*args, **kwargs):
            self.clock.now += .6
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
        port = MockPort()
        service = FollowService(port, tracker=Detector())
        try:
            port.publish((True, 320), 1, time.time())
            service.capture_once()
            self.assertTrue(service.calibrate_reference().ok)
            self.assertTrue(service.start_follow().ok)
            port.publish((True, 550), 2, time.time())
            service.capture_once()
            time.sleep(.11)
            service.control_once()
            self.assertGreater(port.velocity[0], 0)
            service.tracker = BlockingDetector()
            port.publish((True, 550), 3, time.time())
            service.start_background()
            self.assertTrue(entered.wait(1))
            deadline = time.monotonic() + 1.5
            while port.velocity != (0, 0, 0, 0) and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(port.velocity, (0, 0, 0, 0))
            self.assertEqual(service.get_follow_status()["state"], "frame_stale")
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
