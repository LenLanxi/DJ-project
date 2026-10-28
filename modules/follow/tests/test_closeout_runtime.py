"""Closeout cases through real marker pixels, service, and simulated requests."""
import unittest
from unittest.mock import patch

from camera_pid_bench import make_service, displayed_distance
from r3_follow.physical_metrics import create_distance_calibration
from r3_follow.presentation import motion_text, distance_text, depth_status_text
from r3_follow.vision import synthetic_frame


class Clock:
    now = 100.
    def mono(self): return self.now
    def wall(self): return 1800000000. + self.now


class Reader:
    latest = None
    def snapshot(self): return self.latest, ''


class CloseoutRuntimeTests(unittest.TestCase):
    def setUp(self):
        for name in ('socket.socket', 'cv2.VideoCapture', 'tkinter.Tk'):
            guard = patch(name, side_effect=AssertionError('external action forbidden'))
            guard.start(); self.addCleanup(guard.stop)

    def instance(self, mode='yaw', target_id=0):
        clock, reader = Clock(), Reader()
        port, service = make_service(reader, target_id, steering_mode=mode,
                                     monotonic=clock.mono, wall=clock.wall)
        self.addCleanup(service.close)
        def frame(x=.5, size=.25, visible=True, marker_id=target_id):
            clock.now += .1
            seq = max(1, service.last_received_frame_id + 1)
            reader.latest = (seq, clock.now, synthetic_frame(x, size, visible, marker_id))
            service.capture_once(); service.control_once()
        frame()
        self.assertTrue(service.calibrate_reference().ok)
        self.assertTrue(service.start_follow().ok)
        return clock, reader, port, service, frame

    def test_all_modes_direction_loss_recovery_and_explicit_stop(self):
        for mode in ('yaw', 'lateral', 'hybrid'):
            with self.subTest(mode=mode):
                clock, reader, port, service, frame = self.instance(mode)
                for _ in range(6): frame(x=.70, size=.18)
                right, forward, up, yaw = port.velocity
                self.assertGreater(forward, 0)
                self.assertEqual(up, 0)
                self.assertGreater(right, 0) if mode != 'yaw' else self.assertEqual(right, 0)
                self.assertGreater(yaw, 0) if mode != 'lateral' else self.assertEqual(yaw, 0)
                self.assertLessEqual(abs(right) + abs(yaw), 20)
                for _ in range(6): frame(x=.3, size=.32)
                self.assertLess(port.velocity[1], 0)
                for channel in (0, 3): self.assertLessEqual(port.velocity[channel], 0)
                frame(marker_id=1)
                self.assertEqual(port.velocity, (0, 0, 0, 0))
                self.assertTrue(service.get_follow_status()['recovering'])
                for _ in range(5): frame(x=.70, size=.18)
                self.assertTrue(service.is_following())
                self.assertGreater(port.velocity[1], 0)
                service.stop_follow()
                for _ in range(4): frame(x=.70, size=.18)
                self.assertFalse(service.is_following())
                self.assertEqual(port.velocity, (0, 0, 0, 0))

    def test_new_modes_do_not_resume_after_late_action_completion(self):
        for mode in ('lateral', 'hybrid'):
            with self.subTest(mode=mode):
                clock, reader, port, service, frame = self.instance(mode)
                result, ticket = service.pause_for_action()
                self.assertTrue(result.ok)
                self.assertEqual(service.finish_action(ticket, 'accepted').status, 'accepted')
                service.stop_follow()
                for _ in range(4): frame(x=.75, size=.18)
                self.assertEqual(service.finish_action(ticket, 'completed').reason, 'obsolete_ticket')
                self.assertEqual(port.velocity, (0, 0, 0, 0))
                self.assertFalse(service.is_following())

    def test_hybrid_control_loss_does_not_zero_new_owner(self):
        clock, reader, port, service, frame = self.instance('hybrid')
        for _ in range(4): frame(x=.75, size=.18)
        lease = port.revoke_for_test('model')
        port.update_velocity(lease, left_right=5, forward_back=3, up_down=0, yaw=2)
        for _ in range(4): frame(x=.75, size=.18)
        self.assertFalse(service.get_follow_status()['active'])
        self.assertEqual(port.velocity, (5, 3, 0, 2))

    def test_hybrid_stale_stops_and_requires_new_start(self):
        clock, reader, port, service, frame = self.instance('hybrid')
        for _ in range(4): frame(x=.75, size=.18)
        clock.now += 1.01
        service.control_once()
        self.assertEqual(port.velocity, (0, 0, 0, 0))
        for _ in range(4): frame(x=.75, size=.18)
        self.assertTrue(service.get_follow_status()['restart_required'])
        self.assertFalse(service.is_following())

    def test_three_ids_remain_separate_in_hybrid(self):
        instances = [self.instance('hybrid', target_id=i) for i in range(3)]
        for target_id, (_, _, port, service, frame) in enumerate(instances):
            for _ in range(4): frame(x=.75, size=.18)
            self.assertTrue(any(port.velocity))
            frame(marker_id=(target_id + 1) % 3)
            self.assertEqual(port.velocity, (0, 0, 0, 0))
            self.assertFalse(service.get_follow_status()['target_visible'])
            self.assertEqual(service.get_follow_status()['configured_steering_mode'], 'hybrid')

    def test_units_never_claim_measured_velocity(self):
        _, _, _, service, _ = self.instance()
        status = service.get_follow_status()
        self.assertIsNone(status['actual_velocity_m_s'])
        self.assertEqual(status['motion_unit'], 'RC_channel_value_not_physical_velocity')
        self.assertIn('未标定', distance_text({}))
        self.assertIn('估计', distance_text(dict(status='estimated', distance_m=1.5)))

    def test_display_describes_aircraft_motion_not_person(self):
        text = motion_text((-10, -14, 0, -8), 'hybrid')
        for phrase in ('飞机后退：14 RC', '飞机左转：8 RC', '飞机左移：10 RC',
                       '不是米/秒', '方向以飞机看出去为准'):
            self.assertIn(phrase, text)
        text = motion_text((0, 20, 0, 10), 'yaw')
        self.assertIn('飞机前进：20 RC', text)
        self.assertIn('飞机右转：10 RC', text)
        self.assertIn('不横移', text)
        self.assertIn('前后归零', depth_status_text({'reason': 'waiting_for_stable_shape'}))

    def test_distance_label_clears_during_recovery_and_shape_wait(self):
        clock, reader, port, service, frame = self.instance()
        for _ in range(4): frame()
        obs = service.controller.observation
        calibration = create_distance_calibration(obs, 1.5, now=clock.now,
            camera_key='test-camera', marker_key='test-board', front_facing_confirmed=True)
        self.assertEqual(calibration['status'], 'recorded')
        status = service.get_follow_status()
        def label(state):
            return displayed_distance(obs, calibration['calibration'], state,
                now=clock.now, camera_key='test-camera', marker_key='test-board')
        self.assertAlmostEqual(label(status)['distance_m'], 1.5)
        for changes in ({'recovering': True}, {'observation_fresh': False},
                        {'distance_guard': {'forward_allowed': False}}):
            with self.subTest(changes=changes):
                result = label({**status, **changes})
                self.assertEqual(result['status'], 'unavailable')
                self.assertIsNone(result['distance_m'])
