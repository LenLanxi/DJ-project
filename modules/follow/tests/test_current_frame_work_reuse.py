"""Current-frame work reuse must preserve identity checks and deadlines."""
from pathlib import Path
import unittest
from unittest.mock import Mock

import cv2
import numpy as np

from r3_follow.vision import MarkerTracker, synthetic_frame


class CurrentFrameWorkReuseTests(unittest.TestCase):
    def setUp(self):
        cv2.setNumThreads(1)

    def side_fixture(self):
        path = Path(__file__).parent / 'fixtures' / 'side_contour_rescue.png'
        return cv2.imdecode(np.frombuffer(path.read_bytes(), np.uint8), 0)

    def test_side_rescue_reuses_completed_half_contrast_identity_pass(self):
        tracker = MarkerTracker()
        observation = tracker.detect(self.side_fixture(), 1, 10.)
        self.assertTrue(observation.found)
        methods = [a['method'] for a in tracker.last_diagnostics['attempts']]
        self.assertIn('downscaled_local_contrast', methods)
        self.assertIn('edge_quad_exact', methods)
        self.assertNotIn('identity_half_contrast', methods)
        check = tracker.last_diagnostics['identity_check']
        self.assertTrue(check['performed'])
        self.assertTrue(check['reused_current_frame'])
        self.assertEqual(check['saved_decoder_calls'], 1)
        self.assertTrue(tracker.last_diagnostics['duplicate_check']['scan_complete'])

    def test_reuse_never_crosses_frames_or_remaps_another_id(self):
        tracker = MarkerTracker()
        self.assertTrue(tracker.detect(self.side_fixture(), 1, 10.).found)
        current = tracker.detect(synthetic_frame(x=.7), 2, 10.1)
        self.assertTrue(current.found)
        self.assertGreater(current.center_x, 400)
        self.assertFalse(tracker.last_diagnostics['identity_check']['reused_current_frame'])
        methods = [a['method'] for a in tracker.last_diagnostics['attempts']]
        self.assertEqual(methods.count('identity_half_contrast'), 1)
        missing = tracker.detect(synthetic_frame(marker_id=1), 3, 10.2)
        self.assertFalse(missing.found)
        self.assertIsNone(missing.center_x)
        self.assertEqual(missing.corners, ())

    def test_weak_same_id_without_default_quad_still_stops(self):
        dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
        image = np.full((480, 640), 108, np.uint8)
        marker = cv2.aruco.generateImageMarker(dictionary, 0, 120)
        image[180:300, 100:220] = marker
        image[180:300, 420:540] = np.where(marker == 0, 100, 108)
        tracker = MarkerTracker()
        _, ids, rejected = tracker.detector.detectMarkers(image)
        self.assertEqual(ids.flatten().tolist(), [0])
        observation = tracker.detect(image, 1, 10.)
        self.assertFalse(observation.found)
        self.assertEqual(observation.reason, 'ambiguous_duplicate_id')
        self.assertIsNone(observation.center_x)
        self.assertEqual(tracker.last_diagnostics['identity_check']['method'],
                         'identity_half_contrast')

    def test_native_half_contrast_overrun_fails_closed_before_reuse(self):
        elapsed = [0.]
        tracker = MarkerTracker(processing_budget_ms=120, clock=lambda: elapsed[0])
        real = tracker.motion_detector
        tracker.motion_detector = Mock()
        image = self.side_fixture()
        half_shape = cv2.resize(image, None, fx=.5, fy=.5).shape
        half_calls = [0]
        def detect(image):
            result = real.detectMarkers(image)
            if image.shape == half_shape:
                half_calls[0] += 1
                if half_calls[0] == 2:
                    elapsed[0] += .121
            return result
        tracker.motion_detector.detectMarkers.side_effect = detect
        observation = tracker.detect(image, 1, 10.)
        self.assertFalse(observation.found)
        self.assertEqual(observation.reason, 'detection_budget_exceeded')
        self.assertIsNone(observation.center_x)
        self.assertEqual(observation.corners, ())
        self.assertEqual(tracker.last_diagnostics['processing_budget']['limit_ms'], 120)
        self.assertTrue(tracker.last_diagnostics['processing_budget']['exceeded'])
        self.assertEqual(tracker.last_diagnostics['processing_budget']['exhausted_at'],
                         'downscaled_local_contrast:after')
        methods = [a['method'] for a in tracker.last_diagnostics['attempts']]
        self.assertNotIn('edge_quad_exact', methods)


if __name__ == '__main__':
    unittest.main()
