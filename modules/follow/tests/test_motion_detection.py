import json
from pathlib import Path
import unittest
from unittest.mock import Mock

import cv2
import numpy as np

from r3_follow.vision import MarkerTracker, synthetic_frame


class MotionDetectionTests(unittest.TestCase):
    def test_blurred_marker_fixtures_decode_at_expected_position(self):
        folder = Path(__file__).parent / "fixtures"
        cases = json.loads((folder / "motion_markers.json").read_text())
        for name, info in cases.items():
            with self.subTest(name=name):
                tracker = MarkerTracker()
                image = cv2.imdecode(np.frombuffer((folder / name).read_bytes(), np.uint8),
                                     cv2.IMREAD_COLOR)
                obs = tracker.detect(image, 1, 0)
                self.assertTrue(obs.found)
                self.assertLess(np.linalg.norm(
                    np.array([obs.center_x, obs.center_y])-info["expected_center"]), 12)

    def test_new_decoder_does_not_relabel_other_id(self):
        tracker = MarkerTracker()
        tracker.motion_detector = Mock(wraps=tracker.motion_detector)
        obs = tracker.detect(synthetic_frame(marker_id=17), 1, 0)
        self.assertFalse(obs.found)
        self.assertEqual(obs.reason, "not_found")
        self.assertTrue(all(v != 0 for attempt in tracker.last_diagnostics["attempts"]
                            for v in attempt["ids"]))

    def test_duplicate_id_in_new_path_is_rejected(self):
        tracker = MarkerTracker()
        tracker.detector = Mock()
        tracker.fallback_detector = Mock()
        for detector in (tracker.detector, tracker.fallback_detector):
            detector.detectMarkers.return_value = ([], None, [])
        frame = np.minimum(synthetic_frame(x=.2, size=.12),
                           synthetic_frame(x=.8, size=.12))
        obs = tracker.detect(frame, 1, 0)
        self.assertFalse(obs.found)
        self.assertEqual(obs.reason, "ambiguous_duplicate_id")

    def test_blank_frame_never_reuses_previous_target(self):
        tracker = MarkerTracker()
        self.assertTrue(tracker.detect(synthetic_frame(), 1, 0).found)
        obs = tracker.detect(synthetic_frame(visible=False), 2, .1)
        self.assertFalse(obs.found)
        self.assertIsNone(obs.center_x)
        self.assertNotIn("target_quality", tracker.last_diagnostics)

    def test_candidate_locations_and_metadata_are_serializable(self):
        tracker = MarkerTracker()
        tracker.detect(synthetic_frame(marker_id=37), 1, 0)
        details = tracker.last_diagnostics
        self.assertEqual(details["ignored_other_markers"][0]["id"], 37)
        self.assertEqual(len(details["ignored_other_markers"][0]["corners"]), 4)
        self.assertLessEqual(len(details["rejected_quads_sample"]), 6)
        json.dumps(details)
        with self.assertRaises(ValueError):
            tracker.detect(None, 2, .1)
        self.assertEqual(tracker.last_diagnostics, {})
