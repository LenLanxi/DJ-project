from tests.helpers.FollowFixture import make_controller as FollowController
from follow.r3_follow.config import FollowConfig
"""Image-level regressions and truthful current-frame guidance."""
import unittest
from pathlib import Path
import cv2
import numpy as np
from follow.r3_follow.vision import MarkerTracker, synthetic_frame


class VisionContrastTests(unittest.TestCase):
    def image(self):
        path=Path(__file__).resolve().parents[1]/'fixtures'/'follow'/'contrast_rescue.png'
        return cv2.imdecode(np.frombuffer(path.read_bytes(),np.uint8),cv2.IMREAD_GRAYSCALE)

    def test_blurred_low_contrast_image_keeps_correct_identity_and_position(self):
        image=self.image(); original=image.copy()
        for frame in (image, cv2.cvtColor(image,cv2.COLOR_GRAY2BGR),cv2.cvtColor(image,cv2.COLOR_GRAY2BGRA)):
            obs=MarkerTracker().detect(frame,1,0)
            self.assertTrue(obs.found)
            self.assertEqual(obs.target_id,0)
            self.assertLess(np.linalg.norm(np.array([obs.center_x,obs.center_y])-[174.834,128.212]),12)
            self.assertTrue(0<obs.size_ratio<1)
        np.testing.assert_array_equal(image,original)

    def test_all_other_clean_dictionary_ids_are_not_target_zero(self):
        tracker=MarkerTracker()
        for marker_id in range(1,50):
            with self.subTest(marker_id=marker_id):
                obs=tracker.detect(synthetic_frame(marker_id=marker_id),marker_id,0)
                self.assertFalse(obs.found)
                self.assertIsNone(obs.center_x)

    def test_missing_frame_after_rescue_has_no_position_or_quality(self):
        tracker=MarkerTracker();self.assertTrue(tracker.detect(self.image(),1,0).found)
        obs=tracker.detect(np.full_like(self.image(),235),2,.1)
        self.assertFalse(obs.found);self.assertIsNone(obs.center_x)
        self.assertNotIn('target_quality',tracker.last_diagnostics)

    def test_loss_after_contrast_decode_still_zeros_control(self):
        tracker=MarkerTracker();c=FollowController(config=FollowConfig(slew_rc_per_second=None), auto_recover=True)
        first=tracker.detect(self.image(),1,0);c.observe(first);c.calibrate(0);c.start(0)
        next_obs=tracker.detect(np.full_like(self.image(),235),2,.1)
        c.observe(next_obs);r=c.tick(.1)
        self.assertEqual((r.right,r.forward),(0,0))

    def test_border_advice_does_not_reject_valid_target(self):
        frame=np.full((240,320),245,np.uint8)
        dictionary=cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
        frame[70:170,4:104]=cv2.aruco.generateImageMarker(dictionary,0,100)
        tracker=MarkerTracker();obs=tracker.detect(frame,1,0)
        self.assertTrue(obs.found)
        quality=tracker.last_diagnostics['target_quality']
        self.assertTrue(quality['near_image_edge'])
        self.assertAlmostEqual(quality['edge_margin_px'],4,delta=1)

    def test_centered_and_expired_frames_do_not_get_border_advice(self):
        tracker=MarkerTracker();obs=tracker.detect(synthetic_frame(),1,0)
        tracker.last_diagnostics['target_quality']['near_image_edge']=True

    def test_missed_target_does_not_reuse_previous_border_warning(self):
        tracker=MarkerTracker();obs=tracker.detect(synthetic_frame(visible=False),1,0)

    def test_duplicate_target_has_no_follow_position(self):
        tracker=MarkerTracker()
        obs=tracker.detect(np.minimum(synthetic_frame(x=.2,size=.12),synthetic_frame(x=.8,size=.12)),1,0)
        self.assertFalse(obs.found);self.assertIsNone(obs.center_x)
