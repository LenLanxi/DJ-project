"""Current-frame ROI rescue, identity and bounded-work regression checks."""
import json
from pathlib import Path
import unittest
from unittest.mock import Mock

import cv2
import numpy as np

from r3_follow.vision import MarkerTracker, synthetic_frame


class ResolutionRescueTests(unittest.TestCase):
    fixtures = Path(__file__).parent / 'fixtures'

    def fixture(self):
        return cv2.imdecode(np.frombuffer(
            (self.fixtures/'side_contour_rescue.png').read_bytes(), np.uint8), 0)

    def canvas(self, shape=(720, 1280), positions=((100, 200),)):
        frame = np.full(shape, 235, np.uint8)
        fixture = self.fixture()
        for y, x in positions:
            frame[y:y+260, x:x+360] = fixture
        return frame

    def test_same_pixels_on_larger_canvas_retain_current_corners(self):
        base = MarkerTracker().detect(self.fixture(), 1, 0)
        self.assertTrue(base.found)
        for shape in ((720, 1280), (1080, 1920)):
            with self.subTest(shape=shape):
                frame = self.canvas(shape)
                before = frame.copy()
                tracker = MarkerTracker()
                obs = tracker.detect(frame, 23, 1.25, 'synthetic_resolution')
                self.assertTrue(obs.found)
                self.assertEqual((obs.width, obs.height), shape[::-1])
                self.assertEqual((obs.frame_id, obs.acquired_at, obs.source),
                                 (23, 1.25, 'synthetic_resolution'))
                expected = np.asarray(base.corners) + (200, 100)
                np.testing.assert_allclose(obs.corners, expected, atol=1.)
                self.assertAlmostEqual(obs.size_ratio*obs.width,
                                       base.size_ratio*base.width, delta=1)
                np.testing.assert_array_equal(frame, before)
                diag = tracker.last_diagnostics
                self.assertEqual(diag['detector_method'], 'edge_quad_roi_exact')
                self.assertGreater(diag['large_image_rescue']['roi_count'], 0)
                self.assertLessEqual(diag['large_image_rescue']['input_pixels'],
                                     tracker.RESCUE_MAX_PIXELS)
                self.assertTrue(all(a['input_pixels'] <= 512*512
                    for a in diag['attempts'] if a['method'] == 'edge_quad_roi_exact'))
                json.dumps(diag)

    def test_two_separate_rois_do_not_return_first_target(self):
        tracker = MarkerTracker()
        obs = tracker.detect(self.canvas(positions=((80, 80), (400, 800))), 1, 0)
        self.assertFalse(obs.found)
        self.assertEqual(obs.reason, 'ambiguous_duplicate_id')
        self.assertEqual(tracker.last_diagnostics['detected_ids'], [0, 0])
        self.assertIsNone(obs.center_x)

    def test_overlapping_candidate_crops_do_not_duplicate_one_target(self):
        tracker = MarkerTracker()
        frame = self.canvas()
        _, _, quads = tracker.detector.detectMarkers(frame)
        # Distinct candidate contours in the same marker can lead to two
        # overlapping crops. Both decode the same physical target only once.
        q2 = quads[0].copy()
        q2[:, :, 0] += 6
        tracker.detector = Mock()
        tracker.detector.detectMarkers.return_value = ([], None, [quads[0], q2])
        obs = tracker.detect(frame, 1, 0)
        self.assertTrue(obs.found)
        self.assertEqual(tracker.last_diagnostics['detected_ids'], [0])
        self.assertGreaterEqual(tracker.last_diagnostics['large_image_rescue']['roi_count'], 2)

    def test_narrow_wrong_id_is_not_relabelled(self):
        tracker = MarkerTracker(target_id=17)
        obs = tracker.detect(self.canvas(), 1, 0)
        self.assertFalse(obs.found)
        self.assertIsNone(obs.center_x)
        self.assertTrue(any(m['id'] == 0 and m['method'] == 'edge_quad_roi_exact'
                            for m in tracker.last_diagnostics['ignored_other_markers']))

    def test_current_missing_frame_never_reuses_rescued_position(self):
        tracker = MarkerTracker()
        self.assertTrue(tracker.detect(self.canvas(), 1, 0).found)
        obs = tracker.detect(np.full((720, 1280), 235, np.uint8), 2, .1)
        self.assertFalse(obs.found)
        self.assertEqual(obs.corners, ())
        self.assertEqual(tracker.last_diagnostics['large_image_rescue']['roi_count'], 0)

    def test_candidate_budget_exhaustion_is_explicit_not_partial_success(self):
        tracker = MarkerTracker()
        tracker.side_detector = Mock()
        frame = self.canvas((900, 1280),
            tuple((y, x) for y in (0, 300, 600) for x in (0, 400, 800)))
        obs = tracker.detect(frame, 1, 0)
        self.assertFalse(obs.found)
        self.assertEqual(obs.reason, 'rescue_budget_exceeded')
        self.assertTrue(tracker.last_diagnostics['large_image_rescue']['budget_exceeded'])
        tracker.side_detector.detectMarkers.assert_not_called()

    def test_pixel_and_input_budgets_fail_closed(self):
        for setting in ('RESCUE_MAX_PIXELS', 'RESCUE_MAX_INPUT_PIXELS'):
            with self.subTest(setting=setting):
                tracker = MarkerTracker()
                setattr(tracker, setting, 100)
                tracker.side_detector = Mock()
                obs = tracker.detect(self.canvas(), 1, 0)
                self.assertFalse(obs.found)
                self.assertEqual(obs.reason, 'rescue_budget_exceeded')
                tracker.side_detector.detectMarkers.assert_not_called()

    def test_normal_full_image_success_avoids_roi_decoder(self):
        frame = np.full((720, 1280, 3), 235, np.uint8)
        frame[100:580, 200:840] = synthetic_frame()
        tracker = MarkerTracker()
        tracker.side_detector = Mock()
        obs = tracker.detect(frame, 1, 0)
        self.assertTrue(obs.found)
        self.assertEqual(tracker.last_diagnostics['detector_method'], 'default')
        tracker.side_detector.detectMarkers.assert_not_called()

    def test_random_backgrounds_and_wrong_id_do_not_produce_target(self):
        rng = np.random.default_rng(20261024)
        wrong = np.full((720, 1280, 3), 235, np.uint8)
        wrong[100:580, 200:840] = synthetic_frame(marker_id=37)
        for frame in (wrong, rng.integers(0, 256, (720, 1280), dtype=np.uint8),
                      np.full((720, 1280), 0, np.uint8)):
            with self.subTest(shape=frame.shape):
                self.assertFalse(MarkerTracker().detect(frame, 1, 0).found)


if __name__ == '__main__':
    unittest.main()
