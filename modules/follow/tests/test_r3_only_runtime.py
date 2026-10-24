"""Group 3 changes through the UNMODIFIED reviewed SDK and real image detector.

The SDK driver stays in simulation. Socket creation is forbidden. These tests
exercise actual FollowIntegration/Team3FollowBridge, not actual drone response.
"""
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch

from r3_follow.core import State
from r3_follow.mock_port import MockPort
from r3_follow.service import FollowService
from r3_follow.team2_adapter import load_reviewed_stub
from r3_follow.vision import MarkerTracker, synthetic_frame
from simulation_test_support import settle_landing, settle_workers


REPO = Path(__file__).resolve().parents[3]


class ActualSdkYawRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.network = patch("socket.socket", side_effect=AssertionError("No networking in offline tests"))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.sdk = load_reviewed_stub(REPO)
        self.assertTrue(self.sdk.SIMULATED)
        self.assertTrue(self.sdk._driver.snapshot()["simulated"])
        self.assertFalse(self.sdk._driver.snapshot()["connected"])
        self.sdk.stop_follow()
        settle_workers(self.sdk)
        self.sdk.set_frame_provider(None)
        owner = self.sdk.get_status().control_owner
        if owner not in (None, "none"):
            self.sdk.land(owner)
            settle_landing(self.sdk)
            self.sdk.release_control(owner)
        self.assertTrue(self.sdk.request_control("model").ok)
        self.sdk.land("model")
        settle_landing(self.sdk)
        self.assertTrue(self.sdk.takeoff("model").ok)
        self.assertTrue(self.sdk.release_control("model").ok)
        self.feed_lock = threading.Lock()
        self.feed_options = dict(x=.5, size=.25, visible=True)
        self.seq = 0
        self.sdk.set_frame_provider(self.feed)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.sdk.stop_follow()
        settle_workers(self.sdk)
        self.sdk.set_frame_provider(None)
        owner = self.sdk.get_status().control_owner
        if owner in (None, "none"):
            self.sdk.request_control("model")
            owner = "model"
        self.sdk.land(owner)
        settle_landing(self.sdk)
        self.sdk.release_control(owner)

    def feed(self):
        with self.feed_lock:
            self.seq += 1
            frame = synthetic_frame(**self.feed_options)
            return self.sdk.FrameResult(frame, self.seq, time.time(), False)

    def change(self, **options):
        with self.feed_lock:
            self.feed_options.update(options)

    def wait_for(self, condition, timeout=2.):
        deadline = time.monotonic()+timeout
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(.01)
        self.fail("Actual SDK follow condition timed out: " + repr(self.sdk._follow.status()))

    def start(self):
        result = self.sdk.start_follow()
        self.assertTrue(result.ok, result.reason)
        service = self.sdk._follow._bridge.service
        self.assertIsInstance(service.tracker, MarkerTracker)
        self.assertEqual(service.controller.steering_mode, "yaw")
        self.assertTrue(self.sdk.is_following())
        self.assertTrue(all(worker.is_alive() for worker in service.workers))
        return service

    def velocity(self):
        return tuple(self.sdk._driver.snapshot()["velocity"])

    def test_actual_sdk_dispatches_yaw_and_centered_depth_from_marker_images(self):
        service = self.start()
        self.change(x=.8, size=.12)
        self.wait_for(lambda: self.velocity()[3] > 0)
        self.assertEqual(self.velocity()[:3], (0, 0, 0))
        self.assertEqual(service.controller.state, State.TRACKING)
        self.change(x=.2)
        self.wait_for(lambda: self.velocity()[3] < 0)
        self.assertEqual(self.velocity()[:3], (0, 0, 0))
        self.change(x=.5)
        self.wait_for(lambda: self.velocity()[1] > 0 and self.velocity()[3] == 0)
        self.assertEqual((self.velocity()[0], self.velocity()[2]), (0, 0))
        self.assertLessEqual(self.velocity()[1], 30)
        self.assertTrue(self.sdk.stop_follow().ok)
        self.assertEqual(self.velocity(), (0, 0, 0, 0))

    def test_repeated_sdk_start_during_recovery_keeps_original_service_and_deadline(self):
        service = self.start()
        workers = tuple(service.workers)
        original_reference = service.controller.reference_size
        self.change(visible=False)
        self.wait_for(lambda: service.controller.state == State.RECOVERING)
        loss_at = service.controller._loss_at
        self.assertEqual(self.velocity(), (0, 0, 0, 0))
        result = self.sdk.start_follow()
        self.assertTrue(result.ok, result.reason)
        self.assertIs(self.sdk._follow._bridge.service, service)
        self.assertEqual(tuple(service.workers), workers)
        self.assertEqual(service.controller._loss_at, loss_at)
        self.assertEqual(service.controller.reference_size, original_reference)
        self.assertEqual(service.controller.state, State.RECOVERING)
        self.assertEqual(self.sdk.get_status().control_owner, "follow")
        self.change(visible=True, x=.8)
        self.wait_for(lambda: self.sdk.is_following() and self.velocity()[3] > 0)

    def test_actual_handoff_retains_reference_after_completed_action(self):
        service = self.start()
        reference = service.controller.reference_size
        paused, ticket = self.sdk._follow.pause_for_action()
        self.assertTrue(paused.ok, paused.reason)
        self.assertEqual(self.velocity(), (0, 0, 0, 0))
        self.assertTrue(self.sdk.request_control("model").ok)
        self.change(size=.12)
        self.wait_for(lambda: service.controller.observation.size_ratio < reference*.7)
        self.assertFalse(self.sdk._follow.finish_action(ticket, "accepted").ok)
        self.assertEqual(service.controller.state, State.PAUSED)
        self.assertTrue(self.sdk.move_forward("model", 20).ok)
        self.assertTrue(self.sdk.release_control("model").ok)
        result = self.sdk._follow.finish_action(ticket, "completed")
        self.assertTrue(result.ok, result.reason)
        self.assertIs(self.sdk._follow._bridge.service, service)
        self.assertEqual(service.controller.reference_size, reference)
        self.wait_for(lambda: self.velocity()[1] > 0)

    def test_stopped_sdk_service_is_not_revived_by_old_completion(self):
        service = self.start()
        paused, ticket = self.sdk._follow.pause_for_action()
        self.assertTrue(paused.ok, paused.reason)
        self.assertTrue(self.sdk.stop_follow().ok)
        settle_workers(self.sdk)
        result = self.sdk._follow.finish_action(ticket, "completed")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "obsolete_ticket")
        self.assertTrue(service.closed)
        self.assertFalse(any(w.is_alive() for w in service.workers))
        self.assertFalse(self.sdk.is_following())
        self.assertEqual(self.velocity(), (0, 0, 0, 0))
        self.assertEqual(self.sdk.get_status().control_owner, "none")


class ActualMarkerServiceTests(unittest.TestCase):
    def setUp(self):
        self.now, self.seq = 10., 0
        self.port = MockPort(lambda: self.now)
        self.service = FollowService(self.port, monotonic=lambda: self.now,
                                     wall_clock=lambda: 1800000000+self.now,
                                     recovery_window=3, log_directory=False)
        self.addCleanup(self.service.close)
        self.frame()
        self.assertTrue(self.service.calibrate_reference().ok)
        self.assertTrue(self.service.start_follow().ok)

    def frame(self, **options):
        self.now = round(self.now+.11, 8)
        self.seq += 1
        self.port.publish(synthetic_frame(**options), self.seq, 1800000000+self.now)
        self.assertTrue(self.service.capture_once())
        self.service.control_once()

    def test_real_detector_to_service_to_port_turn_depth_loss_and_stop(self):
        self.assertIsInstance(self.service.tracker, MarkerTracker)
        for _ in range(4):
            self.frame(x=.8, size=.12)
        self.assertEqual(self.port.velocity, (0, 0, 0, 20))
        self.frame(x=.5, size=.12)
        self.assertGreater(self.port.velocity[1], 0)
        self.assertEqual(self.port.velocity[3], 0)
        self.frame(visible=False)
        self.assertEqual(self.port.velocity, (0, 0, 0, 0))
        self.assertEqual(self.service.controller.state, State.RECOVERING)
        self.assertTrue(self.service.stop_follow().ok)
        for _ in range(5):
            self.frame(x=.8, size=.12)
        self.assertEqual(self.port.velocity, (0, 0, 0, 0))
        self.assertEqual(self.service.controller.state, State.STOPPED)


if __name__ == "__main__":
    unittest.main()
