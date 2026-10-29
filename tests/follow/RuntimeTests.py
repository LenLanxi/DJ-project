"""Application follow ownership, cancellation and confirmed worker shutdown."""
import threading
import time
import unittest
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

import cv2

from follow.r3_follow import FollowService
from follow.r3_follow.config import load_config
from app.SessionLog import SessionLog
from drone_sdk.contract import FrameResult
from RuntimeFixture import SyntheticFeed, reset, sdk
from SafetyFixture import wait_for
from follow.r3_follow.vision import synthetic_frame


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.follow = reset(flying=True)

    def tearDown(self):
        reset()

    def test_application_log_receives_follow_events_and_outlives_follow_runtime(self):
        with tempfile.TemporaryDirectory() as folder:
            log = SessionLog(Path(folder) / 'Runtime.jsonl')
            runtime = FollowService(sdk, log_sink=log, config=load_config())
            try:
                self.assertTrue(runtime.close().ok)
                self.assertTrue(runtime.session_log.finished.wait(1))
                self.assertTrue(runtime.status()['log']['flush_complete'])
                log({'event': 'application_still_running'})
            finally:
                runtime.close()
                log.close()
            rows = [json.loads(line) for line in log.path.read_text(encoding='utf-8').splitlines()]
            self.assertTrue(any(row.get('service_session_id') == runtime.service_session_id
                                for row in rows))
            self.assertTrue(any(row['event'] == 'application_still_running' for row in rows))

    def test_stop_and_restart_reuse_workers_but_recalibrate_reference(self):
        feed = SyntheticFeed()
        self.follow.set_frame_provider(feed)
        self.assertTrue(self.follow.start().ok)
        service = self.follow
        workers, reference = tuple(service.workers), service.controller.reference_size
        controller, config = service.controller, service.config
        feed.frozen = feed()
        self.assertTrue(wait_for(lambda: service.last_received_frame_id == feed.frozen.frame_id))
        self.assertTrue(self.follow.stop().ok)
        self.assertFalse(self.follow.is_following())
        self.assertEqual(sdk.get_status().control_owner, 'none')
        self.assertTrue(all(worker.is_alive() for worker in workers))
        feed.size = .35
        feed.frozen = None
        self.assertTrue(self.follow.start().ok)
        self.assertEqual(tuple(service.workers), workers)
        self.assertIsNot(service.controller, controller)
        self.assertIs(service.config, config)
        self.assertGreater(service.controller.reference_size, reference)

    def test_restart_accepts_new_resolution_but_not_cached_pre_stop_frame(self):
        feed = SyntheticFeed()
        self.follow.set_frame_provider(feed)
        self.assertTrue(self.follow.start().ok)
        feed.frozen = feed()
        self.assertTrue(wait_for(lambda: self.follow.last_received_frame_id == feed.frozen.frame_id))
        self.assertTrue(self.follow.stop().ok)
        self.assertFalse(self.follow.start(timeout=.03).ok)
        self.assertIsNone(self.follow.controller.reference_size)
        seq = feed.frozen.frame_id
        frame = cv2.resize(synthetic_frame(size=.35), (320, 240))
        def resized():
            nonlocal seq
            seq += 1
            return FrameResult(frame, seq, time.time(), False)
        self.follow.set_frame_provider(resized)
        self.assertTrue(self.follow.start().ok)
        observation = self.follow.controller.observation
        self.assertEqual((observation.width, observation.height), (320, 240))
        self.assertGreater(self.follow.controller.reference_size, .3)

    def test_late_detector_result_cannot_calibrate_the_reset_session(self):
        service = self.follow
        service.clock, service.wall = lambda: 10., lambda: 1800000010.
        packet = FrameResult(synthetic_frame(), 1, service.wall(), False)
        self.follow.set_frame_provider(lambda: packet)
        entered, release = threading.Event(), threading.Event()
        detect = service.tracker.detect
        def blocked(*args, **kwargs):
            entered.set()
            release.wait(2)
            return detect(*args, **kwargs)
        worker = threading.Thread(target=service.capture_once)
        with patch.object(service.tracker, 'detect', side_effect=blocked):
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                self.assertTrue(self.follow.stop().ok)
            finally:
                release.set()
                worker.join(2)
        self.assertIsNone(service.controller.observation)
        self.assertFalse(service.frame_ready)
        self.assertFalse(self.follow.start(timeout=.03).ok)
        self.assertIsNone(service.controller.reference_size)

    def test_cancelled_start_never_creates_workers_or_acquires_control(self):
        cancelled = threading.Event()
        cancelled.set()
        self.follow.set_frame_provider(SyntheticFeed())
        self.assertFalse(self.follow.start(cancelled=cancelled).ok)
        self.assertEqual(self.follow.workers, [])
        self.assertEqual(sdk.get_status().control_owner, 'none')

    def test_stop_cancels_waiting_start_and_blocked_provider_does_not_delay_close(self):
        entered, release = threading.Event(), threading.Event()
        def blocked():
            entered.set()
            release.wait(2)
            return FrameResult(None, 0, time.time(), True)
        self.follow.set_frame_provider(blocked)
        result = []
        starter = threading.Thread(target=lambda: result.append(self.follow.start(timeout=1)))
        starter.start()
        try:
            self.assertTrue(entered.wait(1))
            began = time.monotonic()
            self.assertTrue(self.follow.stop().ok)
            closed = self.follow.close(wait=False)
            self.assertLess(time.monotonic() - began, .3)
            self.assertFalse(closed.ok)
            self.assertIn('follow-vision', closed.reason)
            starter.join(.5)
            self.assertFalse(starter.is_alive())
            self.assertFalse(result[0].ok)
            self.assertFalse(self.follow.is_following())
        finally:
            release.set()
            starter.join(2)
        self.assertTrue(self.follow.close(wait=True).ok)

    def test_finish_waits_internally_for_a_new_frame_and_ticket_remains_opaque(self):
        feed = SyntheticFeed()
        self.follow.set_frame_provider(feed)
        self.assertTrue(self.follow.start().ok)
        feed.frozen = feed()
        self.assertTrue(wait_for(lambda: self.follow.last_frame_id == feed.frozen.frame_id))
        ticket = self.follow.pause_if_active()
        self.assertIsNotNone(ticket)
        result = []
        worker = threading.Thread(target=lambda: result.append(
            self.follow.finish_action(ticket, True, timeout=.5)))
        worker.start()
        try:
            worker.join(.03)
            self.assertTrue(worker.is_alive())
            feed.frozen = None
            worker.join(1)
            self.assertFalse(worker.is_alive())
            self.assertTrue(result[0].ok, result[0].reason)
        finally:
            feed.frozen = None
            worker.join(2)

    def test_failure_cleanup_cannot_stop_follow_owned_by_another_program(self):
        old, current = threading.Event(), threading.Event()
        self.follow.set_frame_provider(SyntheticFeed())
        self.assertTrue(self.follow.start(cancelled=current).ok)
        old.set()
        self.assertTrue(self.follow.stop_cancelled_operation(old).ok)
        self.assertTrue(self.follow.is_following())
        current.set()
        self.assertTrue(self.follow.stop_cancelled_operation(current).ok)
        self.assertFalse(self.follow.is_following())

    def test_failed_stop_retains_program_ownership_until_cleanup_succeeds(self):
        source = threading.Event()
        self.follow.set_frame_provider(SyntheticFeed())
        self.assertTrue(self.follow.start(cancelled=source).ok)
        source.set()
        with self.follow.lock:
            with patch.object(sdk, 'release_control', return_value=sdk.ControlResult(False, 'follow', 'injected')):
                self.assertFalse(self.follow.stop_cancelled_operation(source).ok)
                self.assertFalse(self.follow.stop_cancelled_operation(source).ok)
                self.assertIsNotNone(self.follow.grant)
            self.assertTrue(self.follow.stop_cancelled_operation(source).ok)
        self.assertEqual(sdk.get_status().control_owner, 'none')

    def test_failed_close_retains_program_cleanup_ownership(self):
        source = threading.Event()
        self.follow.set_frame_provider(SyntheticFeed())
        self.assertTrue(self.follow.start(cancelled=source).ok)
        source.set()
        with patch.object(sdk, 'release_control', return_value=sdk.ControlResult(False, 'follow', 'injected')):
            self.assertFalse(self.follow.close().ok)
            self.assertFalse(self.follow.stop_cancelled_operation(source).ok)
            self.assertFalse(self.follow.close().ok)
        self.assertTrue(self.follow.close().ok)
        self.assertEqual(sdk.get_status().control_owner, 'none')

    def test_failed_pause_assigns_cleanup_to_the_program_requesting_handoff(self):
        old, current = threading.Event(), threading.Event()
        self.follow.set_frame_provider(SyntheticFeed())
        self.assertTrue(self.follow.start(cancelled=old).ok)
        with self.follow.lock:
            with patch.object(sdk, 'release_control', return_value=sdk.ControlResult(False, 'follow', 'injected')):
                with self.assertRaisesRegex(RuntimeError, "cleanup_unconfirmed"):
                    self.follow.pause_if_active(cancelled=current)
                self.assertIsNone(self.follow.ticket)
                current.set()
                self.assertFalse(self.follow.stop_cancelled_operation(current).ok)
                old.set()
                self.assertTrue(self.follow.stop_cancelled_operation(old).ok)
                self.assertIsNotNone(self.follow.grant)
                self.assertFalse(self.follow.stop_cancelled_operation(current).ok)
            self.assertTrue(self.follow.stop_cancelled_operation(current).ok)
        self.assertEqual(sdk.get_status().control_owner, 'none')

    def test_start_cancelled_after_grant_retains_failed_cleanup_for_retry(self):
        source = threading.Event()
        self.follow.set_frame_provider(SyntheticFeed())
        start = self.follow._start_tracking
        def started_then_cancelled():
            result = start()
            source.set()
            return result
        with patch.object(self.follow, '_start_tracking', side_effect=started_then_cancelled), \
             patch.object(sdk, 'release_control', return_value=sdk.ControlResult(False, 'follow', 'injected')):
            result = self.follow.start(cancelled=source)
            self.assertFalse(result.ok)
            self.assertEqual(result.reason, 'program_cancelled')
            self.assertFalse(self.follow.stop_cancelled_operation(source).ok)
        self.assertTrue(self.follow.stop_cancelled_operation(source).ok)
        self.assertEqual(sdk.get_status().control_owner, 'none')


    def test_status_queries_do_not_send_control_or_expire_the_session(self):
        self.follow.set_frame_provider(SyntheticFeed(position=.8))
        self.assertTrue(self.follow.start().ok)
        self.assertTrue(wait_for(lambda: sdk.get_status().velocity[3] > 0))
        with self.follow.lock:
            stamp = self.follow.clock()+2
            self.follow.clock = lambda: stamp
            state = sdk.get_status()
            events = self.follow.events()
            for _ in range(5):
                self.assertFalse(self.follow.status()['following'])
                self.assertFalse(self.follow.is_following())
            self.assertEqual(sdk.get_status().velocity, state.velocity)
            self.assertEqual(sdk.get_status().command_epoch, state.command_epoch)
            self.assertEqual(self.follow.events(), events)
            self.follow.control_once()
            self.assertEqual(sdk.get_status().control_owner, 'none')
            self.assertEqual(sdk.get_status().velocity, (0,0,0,0))

    def test_event_snapshot_uses_observations_and_motion_and_is_bounded(self):
        self.follow.set_frame_provider(SyntheticFeed(position=.8))
        self.assertTrue(self.follow.start().ok)
        self.assertTrue(wait_for(lambda: any(e['event']=='motion_request' for e in self.follow.events())))
        with self.follow.lock:
            self.assertIn('observation', {e['event'] for e in self.follow.events()})
            for seq in range(4100):
                self.follow._record('bounded_test', seq=seq)
            snapshot = self.follow.events()
            self.assertEqual(len(snapshot), 4096)
            self.assertEqual(snapshot[-1]['seq'], 4099)
            snapshot.clear()
            self.assertEqual(len(self.follow.events()), 4096)
