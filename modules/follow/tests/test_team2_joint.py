from simulation_test_support import settle_landing, settle_workers
"""Reviewed upstream SDK + actual ArUco detector + local joint-entry candidate."""
from dataclasses import replace
import json
from pathlib import Path
import threading
import time
import unittest
import sys
from unittest.mock import patch

from r3_follow.service_ports import FrameSnapshot
from r3_follow.team2_adapter import Team2StubPort
from r3_follow.team2_joint import JointRuntime
from r3_follow.vision import synthetic_frame

ROOT = Path(__file__).resolve().parents[1]
REPO = (ROOT.parents[1] if (ROOT.parents[1] / 'drone_sdk').is_dir()
        else ROOT.parent / 'tmp' / 'team2_b9f37f9')
AI_ROOT = REPO / 'modules' / 'ai'
if AI_ROOT.is_dir() and str(AI_ROOT) not in sys.path:
    sys.path.insert(0, str(AI_ROOT))
MEASUREMENTS = []


class JointTests(unittest.TestCase):
    def setUp(self):
        self.port = Team2StubPort(REPO)
        self.sdk = self.port.sdk
        self.sdk._driver.set_simulated_delay(0)
        owner = self.sdk.get_status().control_owner
        if owner != "none":
            self.sdk.release_control(owner)
        self.sdk.request_control("model")
        self.sdk.land("model")
        settle_landing(self.sdk)
        self.sdk.takeoff("model")
        self.sdk.release_control("model")
        self.runtime = JointRuntime(self.port)
        self.service = self.runtime.bridge.service
        self.now, self.seq = 10.0, 0
        self.service.clock = lambda: self.now
        self.service.wall = lambda: 1800000000. + self.now
        # This fixture advances simulated time explicitly. Use the same clock
        # for detector admission; CPU-budget behavior has dedicated tests.
        self.service.tracker._clock = self.service.clock
        self.packet = None
        self.port.frame_provider = lambda: self.packet
        self.addCleanup(self.cleanup)
        self.frame()
        self.assertTrue(self.service.calibrate_reference().ok)

    def cleanup(self):
        self.runtime.close()
        owner = self.sdk.get_status().control_owner
        if owner != "none":
            self.sdk.land(owner)
            settle_landing(self.sdk)
            self.sdk.release_control(owner)

    def frame(self, **kwargs):
        self.now += .11
        self.seq += 1
        self.packet = FrameSnapshot(synthetic_frame(**kwargs), self.seq, self.service.wall())
        self.service.capture_once()
        self.service.control_once()

    def following(self):
        self.assertTrue(self.runtime.start_follow()["ok"])
        self.frame(x=.8)
        self.assertTrue(self.runtime.is_following())
        self.assertGreater(self.service.last_channels[3], 0)
        self.assertEqual(self.service.last_channels[0], 0)

    def confirmed_landing(self):
        deadline = time.monotonic() + 1
        result = self.runtime.poll_landing()
        while result["status"] == "accepted" and time.monotonic() < deadline:
            time.sleep(.005)
            result = self.runtime.poll_landing()
        return result

    def test_shared_entry_really_detects_and_controls(self):
        self.following()
        self.assertEqual(self.sdk.request_control("model").reason, "busy")
        self.assertTrue(self.runtime.stop_follow()["ok"])
        self.assertFalse(self.runtime.is_following())
        self.assertEqual(self.sdk.get_status().control_owner, "none")
        self.assertEqual(self.service.last_channels, (0, 0, 0, 0))

    def test_successful_model_handoff_releases_and_conditionally_resumes(self):
        self.following()
        execute = self.sdk._driver.execute
        def moved(*args, **kwargs):
            self.assertFalse(self.runtime.is_following())
            self.assertEqual(self.sdk.get_status().control_owner, "model")
            self.frame(x=.2)
            return execute(*args, **kwargs)
        with patch.object(self.sdk._driver, "execute", side_effect=moved):
            result = self.runtime.run_action("move_forward", 30)
        self.assertTrue(result["ok"])
        self.assertTrue(result["resumed"])
        self.assertEqual(self.sdk.get_status().control_owner, "follow")
        self.assertEqual(self.service.last_channels, (0, 0, 0, 0))
        self.frame(x=.2)
        self.assertLess(self.service.last_channels[3], 0)

    def test_without_new_frame_action_completes_but_follow_does_not_resume(self):
        self.following()
        result = self.runtime.run_action("move_forward", 30)
        self.assertTrue(result["ok"])
        self.assertFalse(result["resumed"])
        self.assertEqual(result["resume_reason"], "resume_requires_prior_follow_and_new_frame")

    def test_invisible_target_during_action_cannot_resume(self):
        self.following()
        with patch.object(self.sdk._driver, "execute", side_effect=lambda *a, **kw: (self.frame(visible=False), True)[1]):
            result = self.runtime.run_action("move_forward", 30)
        self.assertFalse(result["resumed"])
        self.assertFalse(self.runtime.is_following())

    def test_action_failure_cancels_recovery_and_clears_owner(self):
        self.following()
        with patch.object(self.sdk, "move_forward", return_value=self.sdk.CommandResult(False, "timeout")):
            result = self.runtime.run_action("move_forward", 30)
        self.assertEqual(result["status"], "timeout")
        self.frame(x=.8)
        self.assertFalse(self.runtime.is_following())
        self.assertEqual(self.sdk.get_status().control_owner, "none")

    def test_action_exception_cancels_recovery(self):
        self.following()
        with patch.object(self.sdk._driver, "execute", side_effect=RuntimeError("injected")):
            result = self.runtime.run_action("move_forward", 30)
        self.assertFalse(result["ok"])
        self.assertIn("driver_failed", result["reason"])
        self.assertFalse(self.runtime.is_following())

    def test_failed_cleanup_does_not_report_resume_or_success(self):
        self.following()
        real_release = self.port.release_control
        def release(lease):
            if lease.owner == "model":
                raise RuntimeError("release failed")
            return real_release(lease)
        with patch.object(self.sdk, "move_forward", return_value=self.sdk.CommandResult(False, "timeout")), \
             patch.object(self.port, "release_control", side_effect=release):
            result = self.runtime.run_action("move_forward", 30)
        self.assertFalse(result["ok"])
        self.assertFalse(result["release_confirmed"])
        self.assertFalse(self.runtime.is_following())
        self.assertEqual(self.sdk.get_status().control_owner, "model")

    def test_failed_action_cleanup_stop_exception_is_recorded(self):
        self.following()
        original = self.port.stop_motion
        def stop(lease):
            if lease.owner == "model":
                raise RuntimeError("stop failed")
            return original(lease)
        with patch.object(self.sdk, "move_forward", return_value=self.sdk.CommandResult(False, "failed")), \
             patch.object(self.port, "stop_motion", side_effect=stop):
            result = self.runtime.run_action("move_forward", 30)
        self.assertFalse(result["ok"])
        self.assertTrue(any(e["event"] == "failure_cleanup_stop_exception" for e in self.runtime.events))
        self.assertFalse(self.runtime.is_following())

    def test_unexpected_accepted_from_blocking_action_never_resumes(self):
        self.following()
        with patch.object(self.sdk, "move_forward", return_value=self.sdk.CommandResult(True, "accepted")):
            result = self.runtime.run_action("move_forward", 30)
        self.assertFalse(result["ok"])
        self.assertFalse(self.runtime.is_following())

    def test_busy_does_not_release_an_external_model_owner(self):
        self.sdk.request_control("model")
        result = self.runtime.run_action("move_forward", 30)
        self.assertEqual(result["reason"], "busy")
        self.assertEqual(self.sdk.get_status().control_owner, "model")

    def test_invalid_parameters_do_not_pause_follow(self):
        self.following()
        for name, param in (("move_forward", 19), ("move_back", 501),
                            ("move_left", True), ("rotate", 90), ("__dict__", None)):
            self.assertFalse(self.runtime.run_action(name, param)["ok"])
            self.assertTrue(self.runtime.is_following())

    def test_stop_ack_is_not_a_physical_stop_assertion(self):
        self.following()
        result = self.runtime.interrupt()
        self.assertEqual(result["status"], "accepted")
        self.assertFalse(result["motion_stopped_confirmed"])
        self.frame(x=.8)
        self.assertFalse(self.runtime.is_following())

    def test_cleanup_accepts_send_ack_and_confirms_owner_release(self):
        self.following()
        with patch.object(self.sdk, "stop_motion", return_value=self.sdk.CommandResult(True, "accepted")):
            self.assertTrue(self.runtime.stop_follow()["ok"])
        self.assertEqual(self.sdk.get_status().control_owner, "none")

    def test_landing_ack_does_not_confirm_landed_or_allow_motion(self):
        self.following()
        with patch.object(self.sdk, "land", return_value=self.sdk.CommandResult(True, "completed")):
            result = self.runtime.interrupt(land=True)
        self.assertEqual(result["status"], "accepted")
        self.assertFalse(result["landed_confirmed"])
        self.assertEqual(self.runtime.poll_landing()["status"], "accepted")
        with patch.object(self.sdk, "stop_motion", wraps=self.sdk.stop_motion) as stop:
            self.assertFalse(self.runtime.interrupt()["ok"])
            self.assertEqual(stop.call_count, 0)
        self.assertFalse(self.runtime.run_action("move_forward", 30)["ok"])
        self.assertFalse(self.runtime.start_follow()["ok"])
        self.assertFalse(self.runtime.run_action("takeoff")["ok"])

    def test_landing_requires_fresh_post_request_status(self):
        old = self.sdk.get_status()
        with patch.object(self.sdk, "land", return_value=self.sdk.CommandResult(True, "completed")):
            self.runtime.interrupt(land=True)
        for state in (replace(old, flight_state="landed", timestamp=time.time() - 1),
                      replace(old, flight_state="landed", timestamp=self.runtime.landing_sent_at - .01),
                      replace(old, flight_state="landed", timestamp=self.runtime.landing_sent_at),
                      replace(old, flight_state="landed", connected=False, timestamp=time.time()),
                      replace(old, flight_state="unknown", timestamp=time.time())):
            with self.subTest(state=state), patch.object(self.sdk, "get_status", return_value=state):
                self.assertFalse(self.runtime.poll_landing()["landed_confirmed"])
        self.sdk.land("model")
        settle_landing(self.sdk)
        self.assertTrue(self.confirmed_landing()["landed_confirmed"])
        self.assertFalse(self.runtime.is_following())
        self.assertEqual(self.sdk.get_status().control_owner, "none")

    def test_landing_timeout_keeps_motion_blocked(self):
        with patch.object(self.sdk, "land", return_value=self.sdk.CommandResult(True, "completed")):
            self.runtime.interrupt(land=True)
        self.runtime.landing_deadline = time.monotonic() - 1
        self.assertEqual(self.runtime.poll_landing()["status"], "timeout")
        self.assertFalse(self.runtime.start_follow()["ok"])
        self.assertFalse(self.runtime.interrupt()["ok"])
        self.assertFalse(self.runtime.run_action("move_forward", 30)["ok"])

    def test_land_failure_keeps_recovery_disabled(self):
        self.following()
        with patch.object(self.sdk, "land", return_value=self.sdk.CommandResult(False, "failed")):
            self.assertFalse(self.runtime.interrupt(land=True)["ok"])
        self.frame(x=.8)
        self.assertFalse(self.runtime.is_following())
        self.assertFalse(self.runtime.start_follow()["ok"])

    def test_actual_stub_land_still_requires_explicit_status_poll(self):
        self.following()
        self.assertFalse(self.runtime.interrupt(land=True)["landed_confirmed"])
        self.assertEqual(self.runtime.phase, "landing")
        self.assertTrue(self.confirmed_landing()["landed_confirmed"])
        self.frame(x=.8)
        self.assertFalse(self.runtime.start_follow()["ok"])

    def test_target_visibility_is_not_hardcoded(self):
        self.assertTrue(self.runtime.get_status()["target_visible"])
        self.frame(visible=False)
        self.assertFalse(self.runtime.get_status()["target_visible"])
        self.frame()
        self.now += .51
        self.assertFalse(self.runtime.get_status()["target_visible"])

    def test_no_image_from_upstream_does_not_create_synthetic_evidence(self):
        self.port.frame_provider = None
        self.assertFalse(self.service.capture_once())
        self.assertFalse(self.runtime.start_follow()["ok"])

    def test_send_failure_never_keeps_follow_active(self):
        self.following()
        with patch.object(self.sdk, "update_velocity", return_value=self.sdk.CommandResult(False, "failed")):
            self.frame(x=.8)
        self.assertFalse(self.runtime.is_following())

    def test_cached_frame_never_refreshes_age(self):
        self.following()
        self.now += .6
        self.service.capture_once()
        self.service.control_once()
        self.assertFalse(self.runtime.is_following())
        self.assertEqual(self.service.last_channels, (0, 0, 0, 0))

    def blocked_action(self, intervention):
        self.following()
        entered, release, interrupted = threading.Event(), threading.Event(), threading.Event()
        results = {}
        def blocking(*args, **kwargs):
            entered.set()
            if not release.wait(2):
                raise RuntimeError("test barrier timed out")
        def action():
            results["action"] = self.runtime.run_action("move_forward", 30)
        def urgent():
            began = time.perf_counter()
            results["urgent"] = intervention()
            results["latency_ms"] = (time.perf_counter() - began) * 1000
            interrupted.set()
        worker, priority = threading.Thread(target=action), threading.Thread(target=urgent)
        with patch.object(self.sdk._driver, "execute", side_effect=blocking):
            try:
                worker.start()
                self.assertTrue(entered.wait(1))
                self.assertFalse(self.runtime.run_action("move_forward", 30)["ok"])
                priority.start()
                self.assertTrue(interrupted.wait(.5), "Interrupt waited for blocked action")
                self.assertTrue(worker.is_alive(), "Driver was released before interrupt assertion")
                self.assertFalse(self.runtime.start_follow()["ok"])
            finally:
                release.set()
                worker.join(2)
                if priority.ident:
                    priority.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results["action"]["reason"], "cancelled_result_discarded")
        self.frame(x=.8)
        self.assertFalse(self.runtime.is_following())
        MEASUREMENTS.append(dict(interrupt=results["urgent"].get("reason"),
                                 latency_ms=results["latency_ms"],
                                 driver_was_still_blocked=True, physical_stop_verified=False))

    def test_stop_bypasses_blocked_model_action_and_ignores_late_completion(self):
        self.blocked_action(self.runtime.interrupt)

    def test_land_bypasses_blocked_model_action_and_ignores_late_completion(self):
        self.blocked_action(lambda: self.runtime.interrupt(land=True))
        self.assertTrue(self.confirmed_landing()["landed_confirmed"])

    def test_explicit_stop_follow_cancels_blocked_actions_recovery(self):
        self.blocked_action(self.runtime.stop_follow)

    def test_slow_detector_cannot_block_stop_entry(self):
        self.following()
        entered, release, done = threading.Event(), threading.Event(), threading.Event()
        original = self.service.tracker.detect
        def slow(*args, **kwargs):
            entered.set()
            release.wait(2)
            return original(*args, **kwargs)
        self.now += .11
        self.seq += 1
        self.packet = FrameSnapshot(synthetic_frame(x=.8), self.seq, self.service.wall())
        worker = threading.Thread(target=self.service.capture_once)
        priority = threading.Thread(target=lambda: (self.runtime.interrupt(), done.set()))
        with patch.object(self.service.tracker, "detect", side_effect=slow):
            try:
                worker.start()
                self.assertTrue(entered.wait(1))
                priority.start()
                self.assertTrue(done.wait(.5))
                self.assertTrue(worker.is_alive())
            finally:
                release.set()
                worker.join(2)
                if priority.ident:
                    priority.join(2)
        self.assertFalse(self.runtime.is_following())

    def test_current_runtime_uses_actual_follow_entry(self):
        """Current runtime replaces the retired canned Agent/Demo integration."""
        import asyncio
        sys.path.insert(0, str(REPO / 'tests'))
        from RuntimeFixture import SyntheticFeed, container, reset, runtime
        self.runtime.close()
        reset(flying=True)
        feed = SyntheticFeed()
        self.sdk.set_frame_provider(feed)
        app = runtime()
        async def scenario():
            with container([('start_follow', [])]):
                record = await app.run('drone.start_follow()')
            self.assertTrue(record['ok'], record)
            self.assertTrue(self.sdk.is_following())
            before = self.sdk._follow._bridge.service.controller.reference_size
            feed.position = .2
            with container([('move_forward', [30]), ('get_status', [])]):
                record = await app.run('drone.move_forward(30)\nprint(drone.get_status())')
            self.assertTrue(record['ok'], record)
            self.assertEqual(app.gateway.cleanup['status'], 'follow_resumed')
            self.assertTrue(self.sdk.is_following())
            self.assertAlmostEqual(before, self.sdk._follow._bridge.service.controller.reference_size)
            stopped = await app.interrupt(True)
            self.assertTrue(stopped['ok'])
            # Send acknowledgement must not be advertised as physical landing.
            self.assertNotIn('landed_confirmed', stopped)
            self.assertFalse(self.sdk.is_following())
        try:
            asyncio.run(scenario())
        finally:
            reset()


class UpstreamSessionTests(JointTests):
    # Only the test defined here is loaded by the runner (not inherited cases).
    def test_updated_session_dispatches_stop_before_blocked_action_finishes(self):
        import asyncio
        sys.path.insert(0, str(REPO / 'tests'))
        from RuntimeFixture import SyntheticFeed, container, reset, runtime, wait_until
        self.runtime.close()
        reset(flying=True)
        self.sdk.set_frame_provider(SyntheticFeed())
        app = runtime()
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        def block(*args, **kwargs):
            entered.set()
            if not release.wait(2):
                raise RuntimeError('test barrier timeout')
            finished.set()
            return True
        async def scenario():
            self.assertTrue(self.sdk.start_follow().ok)
            with patch.object(self.sdk._driver, 'execute', side_effect=block), container([('move_forward',[30])]):
                turn = asyncio.create_task(app.run('drone.move_forward(30)'))
                try:
                    await wait_until(entered.is_set)
                    result = await asyncio.wait_for(app.interrupt(), .5)
                    self.assertTrue(result['ok'])
                    self.assertFalse(finished.is_set(), 'driver must still be blocked')
                    self.assertFalse(self.sdk.is_following())
                finally:
                    release.set()
                    result = await asyncio.wait_for(turn, 3)
            self.assertFalse(result['ok'])
            self.assertFalse(self.sdk.is_following(), 'late result must not resume follow')
        try:
            asyncio.run(scenario())
        finally:
            reset()

