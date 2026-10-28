import unittest
from pathlib import Path
from unittest.mock import Mock
import cv2
import numpy as np
from follow.r3_follow.vision import MarkerTracker, synthetic_frame


class MultiscaleTests(unittest.TestCase):
    def force_half(self):
        tracker=MarkerTracker()
        tracker.detector=Mock()
        tracker.fallback_detector=Mock()
        for d in (tracker.detector,tracker.fallback_detector):
            d.detectMarkers.return_value=([],None,[])
        actual=tracker.motion_detector
        tracker.motion_detector=Mock()
        tracker.motion_detector.detectMarkers.side_effect=lambda image: (
            ([],None,[]) if image.shape[0]>=480 else actual.detectMarkers(image))
        return tracker

    def test_blurred_fixtures_decode_without_old_coordinates(self):
        for name,xy in (("scale_rescue_3.png",(180.0023,131.3784)),
                        ("scale_rescue_8.png",(182.8998,128.8334))):
            tracker=MarkerTracker()
            image=cv2.imdecode(np.frombuffer(
                (Path(__file__).resolve().parents[1]/"fixtures"/"follow"/name).read_bytes(),np.uint8),cv2.IMREAD_COLOR)
            obs=tracker.detect(image,1,0)
            self.assertTrue(obs.found)
            # Different OpenCV releases may decode on an earlier path. Require
            # correct current-frame identification and geometry, not a fallback.
            self.assertEqual(tracker.last_diagnostics["detected_ids"], [0])
            self.assertLess(np.linalg.norm(np.array([obs.center_x,obs.center_y])-xy),12)
            lost=tracker.detect(np.full_like(image,235),2,.1)
            self.assertFalse(lost.found)
            self.assertIsNone(lost.center_x)

    def test_half_coordinates_and_size_are_restored(self):
        frame=synthetic_frame(x=.75,size=.3)
        original=MarkerTracker().detect(frame,1,0)
        tracker=self.force_half()
        half=tracker.detect(frame,1,0)
        self.assertTrue(half.found)
        self.assertEqual(tracker.last_diagnostics["detector_method"],"downscaled_cell_interior")
        self.assertAlmostEqual(original.center_x,half.center_x,delta=2)
        self.assertAlmostEqual(original.center_y,half.center_y,delta=2)
        self.assertAlmostEqual(original.size_ratio,half.size_ratio,delta=.005)

    def test_duplicate_zero_in_half_path_still_rejected(self):
        frame=np.minimum(synthetic_frame(x=.2,size=.12),synthetic_frame(x=.8,size=.12))
        obs=self.force_half().detect(frame,1,0)
        self.assertFalse(obs.found)
        self.assertEqual(obs.reason,"ambiguous_duplicate_id")

    def test_other_id_in_half_path_is_only_diagnostic(self):
        tracker=self.force_half()
        obs=tracker.detect(synthetic_frame(marker_id=37),1,0)
        self.assertFalse(obs.found)
        self.assertEqual(obs.reason,"not_found")
        self.assertEqual(tracker.last_diagnostics["ignored_other_markers"][0]["id"],37)

    def test_small_input_skips_extra_reduction(self):
        tracker=MarkerTracker()
        tracker.detect(np.full((120,160),235,np.uint8),1,0)
        self.assertNotIn("downscaled_cell_interior",
                         [r["method"] for r in tracker.last_diagnostics["attempts"]])
