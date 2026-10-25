"""实飞图传 HUD：绿框/红点/偏差值与"跟随为什么没在跑"的提示。"""
import unittest

import numpy as np

import follow_hud


def blank_frame(height=480, width=640):
    return np.zeros((height, width, 3), dtype=np.uint8)


def found_observation(corners, center=(320, 240), image=(640, 480), **extra):
    record = {'event': 'observation', 'found': True, 'reason': 'current_observation',
              'center_x': center[0], 'center_y': center[1], 'size_ratio': 0.2,
              'corners': corners, 'image_width': image[0], 'image_height': image[1],
              'detection_ms': 12.0}
    record.update(extra)
    return record


class FollowStatusTextTests(unittest.TestCase):
    def test_not_started_hints_the_voice_command(self):
        text, color = follow_hud.follow_status_text(None)
        self.assertIn('NOT STARTED', text)
        self.assertIn('start following', text)

    def test_running_is_green(self):
        text, color = follow_hud.follow_status_text({'event': 'start'})
        self.assertIn('RUNNING', text)
        self.assertEqual(color, (0, 255, 0))

    def test_stopped_shows_reason_and_asks_to_restart(self):
        text, color = follow_hud.follow_status_text(
            {'event': 'tracking_halted', 'reason': 'recovery_timeout_requires_start'})
        self.assertIn('STOPPED', text)
        self.assertIn('recovery_timeout_requires_start', text)
        self.assertIn('say it again', text)
        self.assertEqual(color, (0, 0, 255))

    def test_pause_is_neither_running_nor_stopped(self):
        text, _color = follow_hud.follow_status_text({'event': 'pause', 'reason': 'tts'})
        self.assertIn('PAUSED', text)
        self.assertIn('tts', text)

    def test_lifecycle_event_names_cover_the_service(self):
        for name in ('start', 'pause', 'stop', 'tracking_halted', 'fault'):
            self.assertIn(name, follow_hud.RUN_LIFECYCLE)


class DrawFollowOverlayTests(unittest.TestCase):
    def test_draws_green_quad_and_red_dot_at_the_marker(self):
        frame = blank_frame()
        corners = [[200, 180], [280, 180], [280, 260], [200, 260]]
        out = follow_hud.draw_follow_overlay(frame, found_observation(corners, center=(240, 220)))
        top_edge = out[180, 200:281]
        self.assertTrue((top_edge[:, 1] == 255).any(), '绿框上边没画出来')
        self.assertTrue((top_edge[:, 0] == 0).any(), '绿框不该有红色')
        around_dot = out[215:226, 235:246]
        self.assertTrue((around_dot[:, :, 2] == 255).any(), '中心红点没画出来')

    def test_center_line_is_drawn(self):
        out = follow_hud.draw_follow_overlay(blank_frame(), None, None, None, None)
        self.assertTrue((out[:, 320, 1] == 255).any())

    def test_scales_corners_from_observation_size_to_frame_size(self):
        frame = blank_frame(240, 320)
        corners = [[800, 600], [1000, 600], [1000, 760], [800, 760]]
        out = follow_hud.draw_follow_overlay(
            frame, found_observation(corners, center=(900, 680), image=(1280, 960)))
        # 1280x960 的观察坐标缩到 320x240（×0.25）：上边落在 y=150、x=200..250
        self.assertTrue((out[150, 200:251, 1] == 255).any(), '缩放后绿框位置不对')
        self.assertTrue((out[140:200, 200, 1] == 255).any(), '缩放后左边没画出来')

    def test_missing_target_does_not_draw_a_box(self):
        out = follow_hud.draw_follow_overlay(
            blank_frame(), {'event': 'observation', 'found': False,
                            'reason': 'waiting_for_stable_target'})
        self.assertFalse((out[:, :, 1] == 255).any() and (out[100:120, 100:120, 1] == 255).any())

    def test_tolerates_missing_or_broken_corners(self):
        for corners in (None, (), [[1, 2]], [[1, 2], ['x', 'y'], [3, 4], [5, 6]]):
            with self.subTest(corners=corners):
                out = follow_hud.draw_follow_overlay(blank_frame(), found_observation(corners))
                self.assertEqual(out.shape, (480, 640, 3))

    def test_none_frame_is_passed_through(self):
        self.assertIsNone(follow_hud.draw_follow_overlay(None, None))

    def test_motion_channels_and_status_appear_without_target(self):
        mot = {'event': 'motion_request', 'channels': [0, 0, 0, -7],
               'state': 'target_lost', 'reason': 'recovery_timeout_requires_start'}
        out = follow_hud.draw_follow_overlay(blank_frame(), None, mot, mot, mot)
        self.assertEqual(out.shape, (480, 640, 3))


if __name__ == '__main__':
    unittest.main()
