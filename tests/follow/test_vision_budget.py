"""Cooperative detector deadlines; native calls are deliberately not preempted."""
import json
from pathlib import Path
import unittest
from unittest.mock import Mock

import cv2
import numpy as np

from follow.r3_follow.vision import MarkerTracker, synthetic_frame


class Clock:
    def __init__(self):
        self.now = 0.

    def __call__(self):
        return self.now

    def advance(self, ms):
        self.now += ms/1000


class VisionBudgetTests(unittest.TestCase):
    def setUp(self):
        cv2.setNumThreads(1)

    def tracker(self, budget=120):
        clock = Clock()
        tracker = MarkerTracker(processing_budget_ms=budget, clock=clock)
        return tracker, clock

    def decoder(self, clock, elapsed, result):
        decoder = Mock()
        def run(image):
            clock.advance(elapsed)
            return result
        decoder.detectMarkers.side_effect = run
        return decoder

    def selected(self, marker_id=0):
        # Match the target actually present in synthetic_frame(), so an
        # independent identity pass sees the same physical object.
        quad = np.float32([[[240, 160], [399, 160], [399, 319], [240, 319]]])
        return [quad], np.int32([[marker_id]]), []

    def assert_no_position(self, observation):
        self.assertFalse(observation.found)
        self.assertIsNone(observation.center_x)
        self.assertIsNone(observation.center_y)
        self.assertIsNone(observation.size_ratio)
        self.assertEqual(observation.corners, ())

    def test_invalid_budget_configuration_is_rejected(self):
        for value in (0, -1, True, np.bool_(True), float('nan'), float('inf'), '120'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                MarkerTracker(processing_budget_ms=value)
        with self.assertRaises(ValueError):
            MarkerTracker(clock=123)

    def test_per_frame_invalid_budget_is_rejected_before_decoder(self):
        tracker = MarkerTracker()
        tracker.detector = Mock()
        for value in (0, -1, True, float('nan'), float('inf'), '5'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                tracker.detect(synthetic_frame(), 1, 0, processing_budget_ms=value)
        tracker.detector.detectMarkers.assert_not_called()

    def test_decoded_target_survives_optional_budget_overrun(self):
        tracker, clock = self.tracker()
        tracker.detector = self.decoder(clock, 435, self.selected())
        tracker.fallback_detector = Mock()
        observation = tracker.detect(synthetic_frame(), 7, 42.5, 'recorded')
        self.assertTrue(observation.found)
        self.assertEqual((observation.frame_id, observation.acquired_at), (7,42.5))
        self.assertTrue(tracker.last_diagnostics['processing_budget']['exceeded'])
        tracker.fallback_detector.detectMarkers.assert_not_called()

    def test_budget_expiry_before_native_work_has_serializable_empty_attempts(self):
        times = iter((0., .150, .150))
        tracker = MarkerTracker(processing_budget_ms=120, clock=lambda: next(times))
        tracker.detector = Mock()
        observation = tracker.detect(synthetic_frame(), 1, 0)
        self.assert_no_position(observation)
        tracker.detector.detectMarkers.assert_not_called()
        self.assertEqual(tracker.last_diagnostics['attempts'], [])
        self.assertEqual(tracker.last_diagnostics['detector_method'], 'not_started')
        self.assertEqual(tracker.last_diagnostics['processing_budget']['exhausted_at'],
                         'grayscale:before')
        json.dumps(tracker.last_diagnostics)

    def test_exhausted_accumulated_budget_skips_expensive_upscale(self):
        tracker, clock = self.tracker(100)
        blank = ([], None, [])
        tracker.detector = self.decoder(clock, 40, blank)
        tracker.fallback_detector = self.decoder(clock, 40, blank)
        tracker.motion_detector = self.decoder(clock, 25, blank)
        tracker.side_detector = Mock()
        observation = tracker.detect(synthetic_frame(visible=False), 1, 0)
        self.assert_no_position(observation)
        self.assertEqual(observation.reason, 'detection_budget_exceeded')
        self.assertEqual([a['method'] for a in tracker.last_diagnostics['attempts']],
                         ['default', 'wide_threshold', 'cell_interior'])
        tracker.fallback_detector.detectMarkers.assert_called_once()
        tracker.side_detector.detectMarkers.assert_not_called()

    def test_runtime_skips_optional_duplicate_rescue(self):
        tracker, clock = self.tracker(50)
        quads, ids, _ = self.selected()
        unknown = np.float32([[[400,100],[480,100],[480,180],[400,180]]])
        tracker.detector = self.decoder(clock, 5, (quads,ids,[unknown]))
        tracker.motion_detector = Mock()
        self.assertTrue(tracker.detect(synthetic_frame(),1,0).found)
        tracker.motion_detector.detectMarkers.assert_not_called()

    def test_decoded_duplicates_remain_ambiguous_in_runtime(self):
        tracker, clock = self.tracker(20)
        quad, _, _ = self.selected()
        tracker.detector = self.decoder(clock,25, (quad+[quad[0]+100],np.int32([[0],[0]]),[]))
        observation = tracker.detect(synthetic_frame(),1,0)
        self.assertFalse(observation.found)
        self.assertEqual(observation.reason,'ambiguous_duplicate_id')

    def test_each_frame_has_fresh_deadline_and_no_previous_position(self):
        tracker, clock = self.tracker(20)
        tracker.detector = self.decoder(clock, 1, self.selected())
        self.assertTrue(tracker.detect(synthetic_frame(), 1, 0).found)
        tracker.detector = self.decoder(clock, 30, ([], None, []))
        lost = tracker.detect(synthetic_frame(visible=False), 2, .1)
        self.assert_no_position(lost)
        tracker.detector = self.decoder(clock, 1, self.selected())
        recovered = tracker.detect(synthetic_frame(), 3, .2)
        self.assertTrue(recovered.found)
        self.assertFalse(tracker.last_diagnostics['processing_budget']['exceeded'])
        self.assertAlmostEqual(tracker.last_diagnostics['processing_budget']['elapsed_ms'], 1)

    def test_unbounded_offline_mode_is_explicit_and_preserves_full_scan(self):
        tracker, clock = self.tracker(None)
        tracker.detector = self.decoder(clock, 435, self.selected())
        self.assertTrue(tracker.detect(synthetic_frame(), 1, 0).found)
        self.assertIsNone(tracker.last_diagnostics['processing_budget']['limit_ms'])
        self.assertFalse(tracker.last_diagnostics['processing_budget']['exceeded'])

    def test_other_id_never_becomes_selected_identity_at_budget_boundary(self):
        tracker, clock = self.tracker(20)
        tracker.detector = self.decoder(clock, 10, self.selected(1))
        tracker.fallback_detector = self.decoder(clock, 15, self.selected(2))
        observation = tracker.detect(synthetic_frame(), 1, 0)
        self.assert_no_position(observation)
        self.assertEqual(observation.target_id, 0)
        self.assertEqual(observation.reason, 'detection_budget_exceeded')

    def test_known_duplicate_still_rejected_with_remaining_budget(self):
        tracker, clock = self.tracker()
        image = np.minimum(synthetic_frame(x=.2, size=.12),
                           synthetic_frame(x=.8, size=.12))
        observation = tracker.detect(image, 1, 0)
        self.assert_no_position(observation)
        self.assertEqual(observation.reason, 'ambiguous_duplicate_id')

    def test_contrast_rescue_precedes_four_times_larger_upscale(self):
        fixture = Path(__file__).resolve().parents[1]/'fixtures'/'follow'/'contrast_rescue.png'
        image = cv2.imdecode(np.frombuffer(fixture.read_bytes(), np.uint8), 0)
        tracker = MarkerTracker()
        observation = tracker.detect(image, 1, 0)
        self.assertTrue(observation.found)
        attempts = [a['method'] for a in tracker.last_diagnostics['attempts']]
        self.assertIn('downscaled_local_contrast', attempts)
        self.assertNotIn('upscaled_wide_threshold', attempts)

    def test_exact_side_rescue_precedes_upscale_without_skipping_identity_check(self):
        fixture = Path(__file__).resolve().parents[1]/'fixtures'/'follow'/'side_contour_rescue.png'
        image = cv2.imdecode(np.frombuffer(fixture.read_bytes(), np.uint8), 0)
        tracker = MarkerTracker()
        observation = tracker.detect(image, 1, 0)
        self.assertTrue(observation.found)
        self.assertEqual(tracker.last_diagnostics['detector_method'], 'edge_quad_exact')
        self.assertIn('duplicate_check', tracker.last_diagnostics)
        self.assertNotIn('upscaled_wide_threshold',
                         [a['method'] for a in tracker.last_diagnostics['attempts']])

    def test_clean_three_ids_remain_available_under_nominal_budget(self):
        image = np.full((480, 960), 245, np.uint8)
        dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
        for marker_id in range(3):
            image[190:290, 110+marker_id*320:210+marker_id*320] = cv2.aruco.generateImageMarker(
                dictionary, marker_id, 100)
        # Deterministic clock avoids turning a busy CI machine into a test
        # of physical latency; real timing is measured in the benchmark.
        for marker_id in range(3):
            tracker = MarkerTracker(marker_id, processing_budget_ms=120, clock=Clock())
            observation = tracker.detect(image, 1, 0)
            self.assertTrue(observation.found)
            self.assertAlmostEqual(observation.center_x, 159.5+marker_id*320, delta=1)


if __name__ == '__main__':
    unittest.main()
