"""Per-instance configuration/isolation; three independent fake devices only.

This suite cannot prove routing to three physical Tello devices. The upstream
SDK is a singleton; using three wrappers of it is not three device connections.
Real generated ArUco scenes are covered separately by the vision suite.
"""
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from follow.r3_follow.config import FollowConfig, load_config
from follow.r3_follow.core import Observation, State
from tests.helpers.FollowFixture import FakeDevice
from follow.r3_follow import FollowService
from drone_sdk.contract import CommandResult
from follow.r3_follow.config import load_config
from follow.r3_follow import FollowService
from drone_sdk import controller as Device
from follow.r3_follow import FollowService
from follow.r3_follow.config import load_config
from follow.r3_follow.vision import synthetic_frame
from FollowSupport import settle_landing, settle_workers


class GeometryDetector:
    def __init__(self, target_id):
        self.target_id = target_id

    def detect(self, frame, frame_id, acquired_at, source):
        found, x, size, observed_id = frame
        half = size / 2
        corners = ((x-half, 240-half), (x+half, 240-half),
                   (x+half, 240+half), (x-half, 240+half)) if found else ()
        return Observation(frame_id, acquired_at, source, 640, 480,
                           observed_id, found, x, 240, size/640,
                           corners=corners, reason="" if found else "not_found")


class Instance:
    def __init__(self, target_id, *, profile="openpose_pid", label=None, log_directory=False):
        self.t, self.seq = 10., 0
        self.port = FakeDevice(lambda: self.t)
        self.service = FollowService(self.port, config=FollowConfig(target_id=target_id, instance_id=label or f'aircraft-{target_id}', control_profile=profile, max_request_rate=.8),
                           tracker=GeometryDetector(target_id),
                           monotonic=lambda: self.t,
                           wall_clock=lambda: 1800000000 + self.t,
                           recovery_window=3,
                           log_directory=log_directory)

    def frame(self, *, x=320, size=160, found=True, advance=.1, observed_id=None):
        self.t += advance
        self.seq += 1
        observed_id = self.service.config.target_id if observed_id is None else observed_id
        self.port.publish((found, x, size, observed_id), self.seq, 1800000000+self.t)
        result = self.service.capture_once()
        self.service.control_once()
        return result

    def start(self, size=160):
        self.frame(size=size)
        assert self.service.calibrate_reference().ok
        assert self.service._start_tracking().ok


class ConfigTests(unittest.TestCase):
    def config(self, **changes):
        value = dict(instance_id="aircraft-A", target_id=0,
                     dictionary="DICT_4X4_50", control_profile="openpose_pid")
        value.update(changes)
        return value

    def test_no_environment_preserves_legacy_id_zero(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(load_config(), FollowConfig())

    def test_explicit_files_require_all_identity_fields(self):
        for missing in self.config():
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as directory:
                value = self.config()
                del value[missing]
                path = Path(directory) / "invalid.json"
                path.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "missing"):
                    load_config(path)

    def test_invalid_ids_labels_profiles_and_distance_notes_rejected(self):
        bad = [("target_id", v) for v in (-1, 50, True, False, 1.0, "1", None)]
        bad += [("instance_id", v) for v in ("", " ", " A", "A\n", "A/B", None)]
        bad += [("dictionary", "DICT_6X6_250"), ("control_profile", "pid_typo")]
        bad += [("physical_distance_m", v) for v in (-1, 0, True, "2", float("nan"), float("inf"))]
        for key, value in bad:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                load_config(self.config(**{key: value}))

    def test_unknown_duplicate_or_nonobject_json_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown"):
            load_config(self.config(target=2))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            for value in ('[]', '{"target_id": 0, "target_id": 1}'):
                path.write_text(value, encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_config(path)

    def test_environment_file_loaded_each_time_and_missing_file_not_silenced(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "中文 空格.json"
            path.write_text(json.dumps(self.config(target_id=2)), encoding="utf-8-sig")
            with patch.dict(os.environ, {"R3_FOLLOW_CONFIG": str(path)}):
                self.assertEqual(load_config().target_id, 2)
                path.write_text(json.dumps(self.config(target_id=1)), encoding="utf-8")
                self.assertEqual(load_config().target_id, 1)
            with patch.dict(os.environ, {"R3_FOLLOW_CONFIG": str(path.with_name("missing.json"))}):
                with self.assertRaises(FileNotFoundError):
                    load_config()
            with patch.dict(os.environ, {"R3_FOLLOW_CONFIG": ""}):
                with self.assertRaises(ValueError):
                    load_config()

    def test_three_example_files_bind_matching_tracker_and_controller(self):
        folder = (Path(__file__).resolve().parents[2] / 'follow') / "configs"
        for label, target_id in zip("ABC", range(3)):
            with self.subTest(target_id=target_id):
                config = load_config(folder / f"aircraft_{label}_ID{target_id}.json")
                self.assertEqual(config.target_id, target_id)
                self.assertEqual(config.control_profile, "openpose_pid")
                service = FollowService(FakeDevice(), config=config,
                              log_directory=False)
                self.addCleanup(service.close)
                self.assertEqual(service.tracker.target_id, target_id)
                self.assertEqual(service.controller.target_id, target_id)
                self.assertEqual(service.status()["target_id"], target_id)

    def test_bridge_uses_environment_without_upstream_constructor_change(self):
        folder = (Path(__file__).resolve().parents[2] / 'follow') / "configs"
        with patch.dict(os.environ, {"R3_FOLLOW_CONFIG": str(folder / "aircraft_B_ID1.json")}):
            bridge = FollowService(FakeDevice(), config=load_config())
            self.addCleanup(bridge.close)
            status = bridge.status()
            self.assertEqual(status["target_id"], 1)
            self.assertEqual(status["configured_control_profile"], "openpose_pid")
            self.assertEqual(status["instance_id"], "aircraft-B")

    def test_explicit_config_overrides_environment_and_is_frozen(self):
        config = load_config(self.config(target_id=2, physical_distance_m=1.5))
        with patch.dict(os.environ, {"R3_FOLLOW_CONFIG": "missing-file.json"}):
            self.assertIs(load_config(config), config)
            bridge = FollowService(FakeDevice(), config=config)
            self.addCleanup(bridge.close)
            self.assertEqual(bridge.status()["physical_distance_m"], 1.5)
        with self.assertRaises(AttributeError):
            config.target_id = 0

    def test_tracker_explicit_id_mismatch_rejected_before_any_request(self):
        port = FakeDevice()
        for target in (0, True, 1.0):
            with self.subTest(target=target), self.assertRaises(ValueError):
                FollowService(port, config=FollowConfig(target_id=1, max_request_rate=.8),
                    tracker=GeometryDetector(target))
        self.assertEqual(port.events, [])


class MultiInstanceTests(unittest.TestCase):
    def make_three(self, profile="openpose_pid"):
        instances = [Instance(i, profile=profile) for i in range(3)]
        for instance in instances:
            self.addCleanup(instance.service.close)
            instance.start(size=100+40*instance.service.config.target_id)
        return instances

    def test_references_frame_ids_and_timestamps_are_independent(self):
        a, b, c = self.make_three()
        self.assertEqual([i.service.last_frame_id for i in (a,b,c)], [1,1,1])
        self.assertEqual([i.service.controller.reference_size for i in (a,b,c)],
                         [100/640, 140/640, 180/640])
        a.frame(size=80)
        b.frame(size=120)
        self.assertEqual(c.service.last_frame_id, 1)
        self.assertAlmostEqual(c.service.last_timestamp, 1800000010.1)
        self.assertEqual(a.service.controller.reference_size, 100/640)
        self.assertEqual(b.service.controller.reference_size, 140/640)
        self.assertEqual(c.service.controller.reference_size, 180/640)

    def test_pause_stop_and_failure_only_affect_the_selected_instance(self):
        for action in ("pause", "stop", "send_failure", "lost", "expired"):
            with self.subTest(action=action):
                a,b,c = self.make_three(profile="openpose_pid")
                for instance in (a,b,c):
                    for _ in range(4):
                        instance.frame(x=400, size=60)
                    self.assertNotEqual(instance.port.velocity, (0,0,0,0))
                preserved = [(i.port.velocity, i.service.controller.reference_size,
                              i.service.grant, len(i.port.events)) for i in (b,c)]
                if action == "pause": a.service.pause_if_active()
                elif action == "stop": a.service._stop_tracking()
                elif action == "lost": a.frame(found=False)
                elif action == "expired":
                    a.t += 1.1
                    a.service.control_once()
                else:
                    with patch.object(a.port, "update_velocity", return_value=CommandResult(False, "failed", "test_failure")):
                        a.frame(x=400, size=60)
                self.assertEqual(a.port.velocity, (0,0,0,0))
                self.assertEqual(preserved, [(i.port.velocity, i.service.controller.reference_size,
                                              i.service.grant, len(i.port.events)) for i in (b,c)])

    def test_wrong_target_observation_faults_instead_of_tracking_another_id(self):
        a,b,c = self.make_three()
        for instance in (a,b,c):
            instance.frame(x=500)
            self.assertNotEqual(instance.port.velocity, (0,0,0,0))
            self.assertFalse(instance.frame(x=500, observed_id=(instance.service.config.target_id+1)%3))
            self.assertEqual(instance.port.velocity, (0,0,0,0))
            self.assertFalse(instance.service.status()["active"])
            self.assertIsNone(instance.service.grant)

    def test_equal_generation_and_frame_id_cannot_exchange_handoff_tickets(self):
        a,b,c = self.make_three()
        tickets = [i.service.pause_if_active() for i in (a,b,c)]
        self.assertEqual(len({id(t) for t in tickets}), 3)
        for n, instance in enumerate((a,b,c)):
            instance.frame(x=400, size=60)
            result = instance.service.finish_action(tickets[(n+1)%3], True)
            self.assertFalse(result.ok)
            self.assertEqual(result.reason, "obsolete_ticket")
            self.assertEqual(instance.service.ticket, tickets[n])
            self.assertEqual(instance.port.velocity, (0,0,0,0))
            self.assertTrue(instance.service.finish_action(tickets[n], True).ok)

    def test_same_instance_label_and_recreated_service_still_reject_old_notification(self):
        a = Instance(0, label="same-label")
        b = Instance(0, label="same-label")
        for instance in (a,b):
            self.addCleanup(instance.service.close)
            instance.start()
        old_ticket = a.service.pause_if_active()
        new_ticket = b.service.pause_if_active()
        self.assertIsNot(old_ticket, new_ticket)
        b.frame()
        self.assertFalse(b.service.finish_action(old_ticket, True).ok)
        self.assertTrue(b.service.finish_action(new_ticket, True).ok)
        a.service._stop_tracking()
        a.frame()
        self.assertFalse(a.service.finish_action(old_ticket, True).ok)

    def test_unscoped_legacy_ticket_and_late_stop_completion_rejected(self):
        a,b,c = self.make_three()
        ticket = a.service.pause_if_active()
        unscoped = object()
        a.frame()
        self.assertFalse(a.service.finish_action(unscoped, True).ok)
        a.service._stop_tracking()
        self.assertFalse(a.service.finish_action(ticket, True).ok)
        self.assertEqual(a.port.velocity, (0,0,0,0))
        self.assertTrue(b.service.is_following())
        self.assertTrue(c.service.is_following())

    def test_accepted_action_cannot_resume_and_failed_action_clears_own_ticket(self):
        a,b,c = self.make_three()
        ticket = a.service.pause_if_active()
        a.frame()
        self.assertEqual(a.port.velocity, (0,0,0,0))
        self.assertFalse(a.service.finish_action(ticket, False).ok)
        self.assertIsNone(a.service.ticket)
        self.assertEqual(a.port.velocity, (0,0,0,0))
        self.assertTrue(b.service.is_following())
        self.assertTrue(c.service.is_following())

    def test_stop_one_service_does_not_reset_other_controller_response(self):
        a,b,c = self.make_three(profile="openpose_pid")
        for instance in (a,b,c):
            for _ in range(4):
                instance.frame(x=410, size=60)
        before = b.service.controller.last_request
        a.service._stop_tracking()
        self.assertEqual(b.service.controller.last_request, before)
        self.assertIsNot(a.service.controller, b.service.controller)
        b.frame(x=410, size=60)
        self.assertGreater(b.port.velocity[3], 0)
        self.assertGreater(b.port.velocity[1], 0)

    def test_logs_status_and_memory_events_carry_instance_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            instances = [Instance(i, log_directory=directory) for i in range(3)]
            for instance in instances:
                instance.start()
                instance.frame(x=400)
                status = instance.service.status()
                self.assertEqual(status["target_id"], instance.service.config.target_id)
                self.assertEqual(status["dictionary"], "DICT_4X4_50")
                self.assertEqual(status["physical_distance_source"], "manual_note_not_estimated")
                instance.service.close()
                for event in instance.service.events():
                    self.assertEqual(event["service_session_id"], status["service_session_id"])
                    self.assertEqual(event["target_id"], status["target_id"])
            paths = list(Path(directory).glob("*.jsonl"))
            self.assertEqual(len(paths), 3)
            headers = [json.loads(path.read_text(encoding="utf-8").splitlines()[0]) for path in paths]
            self.assertEqual({h["target_id"] for h in headers}, {0,1,2})
            self.assertEqual(len({h["service_session_id"] for h in headers}), 3)
            self.assertEqual(len({h["session_id"] for h in headers}), 3)


class CandidateSdkTests(unittest.TestCase):
    """One unmodified simulated SDK, restarted for each configured target.

    This deliberately does not claim that the singleton SDK routes three
    aircraft at once. Three independent services are tested above using mocks.
    """
    def setUp(self):
        network = patch("socket.socket", side_effect=AssertionError("offline SDK test"))
        network.start()
        self.addCleanup(network.stop)
        self.sdk = Device
        self.follow = FollowService(self.sdk, config=load_config())
        self.assertTrue(self.sdk._driver.snapshot()["simulated"])
        self.assertFalse(self.sdk._driver.snapshot()["connected"])
        self.follow.stop()
        settle_workers(self.follow)
        self.follow.set_frame_provider(None)
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
        self.options = dict(marker_id=0, x=.5, size=.25)
        self.seq = 0
        self.follow.set_frame_provider(self.feed)
        self.addCleanup(self.cleanup)

    def feed(self):
        with self.feed_lock:
            self.seq += 1
            frame = synthetic_frame(**self.options)
            return self.sdk.FrameResult(frame, self.seq, time.time(), False)

    def change(self, **options):
        with self.feed_lock:
            self.options.update(options)

    def velocity(self):
        return tuple(self.sdk._driver.snapshot()["velocity"])

    def wait_for(self, predicate):
        deadline = time.monotonic()+2.5
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.01)
        self.fail("candidate SDK condition timed out: " + repr(self.follow.status()))

    def cleanup(self):
        self.follow.close(wait=True)
        settle_workers(self.follow)
        self.follow.set_frame_provider(None)
        owner = self.sdk.get_status().control_owner
        if owner in (None, "none"):
            self.sdk.request_control("model")
            owner = "model"
        self.sdk.land(owner)
        settle_landing(self.sdk)
        self.sdk.release_control(owner)

    def config_path(self, target_id):
        folder = (Path(__file__).resolve().parents[2] / 'follow') / "configs"
        return str(folder / f"aircraft_{'ABC'[target_id]}_ID{target_id}.json")

    def test_environment_candidate_reaches_unmodified_sdk_for_each_target_and_rejects_wrong_id(self):
        for target_id in range(3):
            with self.subTest(target_id=target_id), patch.dict(os.environ,
                    {"R3_FOLLOW_CONFIG": self.config_path(target_id)}):
                self.follow.close(wait=True)
                self.follow = FollowService(self.sdk, frame_provider=self.feed, config=load_config())
                self.change(marker_id=target_id, x=.5, size=.25)
                result = self.follow.start()
                self.assertTrue(result.ok, result.reason)
                service = self.follow
                self.assertEqual(service.config.control_profile, "openpose_pid")
                self.assertEqual(service.controller.target_id, target_id)
                self.assertEqual(service.tracker.target_id, target_id)
                self.change(x=.7, size=.12)
                self.wait_for(lambda: self.velocity()[1] > 0 and self.velocity()[3] > 0)
                self.assertEqual((self.velocity()[0], self.velocity()[2]), (0,0))
                # 预算按该机自己的配置判定（横向/升降恒为 0 才是不可放松的契约）
                self.assertLessEqual(self.velocity()[1],
                                     round(service.config.forward_limit * 100))
                self.assertLessEqual(self.velocity()[3],
                                     round(service.config.yaw_limit * 100))
                self.change(marker_id=(target_id+1)%3)
                self.wait_for(lambda: not service.controller.observation.found
                                      and self.velocity() == (0,0,0,0))
                self.assertEqual(service.controller.target_id, target_id)
                self.assertTrue(self.follow.stop().ok)
                self.follow.close(wait=True)
                settle_workers(self.follow)
                self.assertEqual(self.velocity(), (0,0,0,0))
                self.assertEqual(self.sdk.get_status().control_owner, "none")

    def test_candidate_sdk_handoff_keeps_reference_and_stop_rejects_late_notification(self):
        with patch.dict(os.environ, {"R3_FOLLOW_CONFIG": self.config_path(2)}):
            self.follow.close(wait=True)
            self.follow = FollowService(self.sdk, frame_provider=self.feed, config=load_config())
            self.change(marker_id=2)
            self.assertTrue(self.follow.start().ok)
            service = self.follow
            reference = service.controller.reference_size
            ticket = self.follow.pause_if_active()
            self.assertIsNotNone(ticket)
            self.assertEqual(self.velocity(), (0,0,0,0))
            self.assertTrue(self.sdk.request_control("model").ok)
            self.change(x=.65, size=.12)
            self.wait_for(lambda: service.controller.observation.size_ratio < reference*.7)
            self.assertEqual(self.velocity(), (0,0,0,0))
            self.assertTrue(self.sdk.move_forward("model", 20).ok)
            self.assertTrue(self.sdk.release_control("model").ok)
            self.assertTrue(self.follow.finish_action(ticket, True).ok)
            self.assertEqual(service.controller.reference_size, reference)
            self.wait_for(lambda: self.velocity()[1] > 0 and self.velocity()[3] > 0)
            late = self.follow.pause_if_active()
            self.assertIsNotNone(ticket)
            self.assertTrue(self.follow.stop().ok)
            self.follow.close(wait=True)
            settle_workers(self.follow)
            self.assertFalse(self.follow.finish_action(late, True).ok)
            self.assertEqual(self.velocity(), (0,0,0,0))
            self.assertEqual(self.sdk.get_status().control_owner, "none")


if __name__ == "__main__":
    unittest.main()
