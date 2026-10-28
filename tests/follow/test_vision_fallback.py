import unittest
from unittest.mock import Mock
import numpy as np
import cv2
from follow.r3_follow.vision import MarkerTracker, synthetic_frame


class FallbackTests(unittest.TestCase):
    def test_upscaled_corners_return_to_original_coordinates(self):
        frame=synthetic_frame(x=.7,size=.15)
        reference=MarkerTracker().detect(frame,1,0,'test')
        tracker=MarkerTracker()
        tracker.detector=Mock()
        tracker.detector.detectMarkers.return_value=([],None,[])
        real=tracker.fallback_detector
        tracker.fallback_detector=Mock()
        tracker.fallback_detector.detectMarkers.side_effect=[([],None,[]),
            real.detectMarkers(cv2.resize(frame,None,fx=2,fy=2,interpolation=cv2.INTER_CUBIC))]
        result=tracker.detect(frame,1,0,'test')
        self.assertTrue(result.found)
        self.assertAlmostEqual(result.center_x,reference.center_x,delta=1)
        self.assertAlmostEqual(result.size_ratio,reference.size_ratio,delta=.005)

    def test_other_id_does_not_block_target_fallback(self):
        tracker=MarkerTracker()
        tracker.detector=Mock()
        tracker.detector.detectMarkers.return_value=(
            [np.float32([[[20,20],[40,20],[40,40],[20,40]]])],
            np.int32([[37]]), [])
        result=tracker.detect(synthetic_frame(),1,0,'test')
        self.assertTrue(result.found)
        self.assertEqual(tracker.last_diagnostics["detector_method"],"wide_threshold")
        self.assertEqual(tracker.last_diagnostics["ignored_other_markers"][0]["id"],37)

    def test_target_and_other_id_in_same_frame(self):
        tracker=MarkerTracker()
        frame=np.minimum(synthetic_frame(x=.2,size=.12),
                         synthetic_frame(x=.8,size=.12,marker_id=37))
        result=tracker.detect(frame,1,0,'test')
        self.assertTrue(result.found)
        self.assertLess(result.center_x,320)

    def test_fallback_duplicate_targets_are_rejected(self):
        tracker=MarkerTracker()
        tracker.detector=Mock()
        tracker.detector.detectMarkers.return_value=([],None,[])
        frame=np.minimum(synthetic_frame(x=.2,size=.12),synthetic_frame(x=.8,size=.12))
        result=tracker.detect(frame,1,0,'test')
        self.assertFalse(result.found)
        self.assertEqual(result.reason,'ambiguous_duplicate_id')
