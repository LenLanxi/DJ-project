"""Image-level and fail-closed checks for narrow marker quad decoding."""
import json
from pathlib import Path
import unittest
from unittest.mock import Mock

import cv2
import numpy as np

from r3_follow.core import FollowController, State
from r3_follow.vision import MarkerTracker, synthetic_frame


class SideViewDetectionTests(unittest.TestCase):
    fixtures = Path(__file__).parent/'fixtures'

    def image(self):
        return cv2.imdecode(np.frombuffer((self.fixtures/'side_contour_rescue.png').read_bytes(),
                                         np.uint8), cv2.IMREAD_GRAYSCALE)

    def reference(self):
        image = np.full((260, 360), 235, np.uint8)
        dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
        image[90:170, 140:220] = cv2.aruco.generateImageMarker(dictionary, 0, 80)
        return image

    def test_failed_fixture_decodes_correct_current_corners(self):
        # A deterministic synthetic image; not a saved camera photograph.
        meta = json.loads((self.fixtures/'side_contour_rescue.json').read_text())
        expected = np.array(meta['expected_corners'])
        tracker = MarkerTracker()
        obs = tracker.detect(self.image(), 19, 12.5, 'fixture')
        self.assertTrue(obs.found)
        self.assertEqual(tracker.last_diagnostics['detector_method'], 'edge_quad_exact')
        self.assertLess(np.max(np.linalg.norm(np.array(obs.corners)-expected, axis=1)), 2)
        self.assertEqual((obs.frame_id, obs.acquired_at, obs.source), (19, 12.5, 'fixture'))

    def test_fallback_handles_gray_bgr_bgra_without_mutating_pixels(self):
        gray = self.image()
        for image in (gray, cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR),
                      cv2.cvtColor(gray, cv2.COLOR_GRAY2BGRA)):
            before = image.copy()
            obs = MarkerTracker().detect(image, 1, 0)
            self.assertTrue(obs.found)
            self.assertAlmostEqual(obs.center_x, 185.79, delta=2)
            np.testing.assert_array_equal(before, image)

    def test_normal_success_does_not_run_extra_decoder(self):
        tracker = MarkerTracker()
        tracker.side_detector = Mock()
        self.assertTrue(tracker.detect(synthetic_frame(), 1, 0).found)
        tracker.side_detector.detectMarkers.assert_not_called()

    def test_two_narrow_target_copies_are_ambiguous(self):
        image = np.concatenate([self.image(), self.image()], axis=1)
        tracker = MarkerTracker()
        obs = tracker.detect(image, 1, 0)
        self.assertFalse(obs.found)
        self.assertIsNone(obs.center_x)
        self.assertEqual(obs.reason, 'ambiguous_duplicate_id')

    def test_missing_frame_after_rescue_never_reuses_position(self):
        tracker = MarkerTracker()
        self.assertTrue(tracker.detect(self.image(), 1, 0).found)
        obs = tracker.detect(np.full_like(self.image(), 235), 2, .1)
        self.assertFalse(obs.found)
        self.assertIsNone(obs.center_x)
        self.assertEqual(obs.corners, ())
        self.assertNotIn('target_quality', tracker.last_diagnostics)

    def test_decoded_side_view_still_blocks_depth_and_loss_stops_both_axes(self):
        tracker = MarkerTracker()
        controller = FollowController(recovery_window=3, max_request_rate=.8,
                                      require_geometry=True)
        controller.observe(tracker.detect(self.reference(), 0, 0))
        self.assertTrue(controller.calibrate(0)[0])
        self.assertTrue(controller.start(0)[0])
        # Move the marker horizontally; its CURRENT corner shape is still tilted.
        image = cv2.warpAffine(self.image(), np.float32([[1, 0, 65], [0, 1, 0]]),
                               (360, 260), borderValue=235)
        for i in range(1, 7):
            obs = tracker.detect(image, i, i*.1)
            self.assertTrue(obs.found)
            controller.observe(obs)
            motion = controller.tick(i*.1)
            self.assertEqual(motion.forward, 0)
            self.assertEqual(controller.distance_guard.reason, 'shape_ambiguous')
        self.assertGreater(motion.right, 0)
        controller.observe(tracker.detect(np.full_like(image, 235), 7, .7))
        motion = controller.tick(.7)
        self.assertEqual((motion.right, motion.forward), (0, 0))
        self.assertEqual(controller.state, State.RECOVERING)

    def test_side_view_cannot_be_used_as_distance_reference(self):
        tracker = MarkerTracker()
        c = FollowController(require_geometry=True)
        c.observe(tracker.detect(self.image(), 1, 0))
        self.assertFalse(c.calibrate(0)[0])
        self.assertIsNone(c.reference_size)

    def test_narrow_pixels_quality_is_current_and_serializable(self):
        tracker = MarkerTracker()
        tracker.detect(self.image(), 1, 0)
        quality = tracker.last_diagnostics['target_quality']
        self.assertLess(quality['min_side_px'], 16)
        self.assertGreater(quality['max_side_px'], 70)
        self.assertFalse(quality['near_image_edge'])
        json.dumps(tracker.last_diagnostics)

    def test_other_id_and_target_selection_are_not_relabelled(self):
        for target in (0, 17):
            tracker = MarkerTracker(target_id=target)
            self.assertFalse(tracker.detect(synthetic_frame(marker_id=37), 1, 0).found)
            self.assertTrue(tracker.detect(synthetic_frame(marker_id=target), 2, .1).found)

    def test_large_images_do_not_add_unbounded_fallback_work(self):
        tracker = MarkerTracker()
        tracker.side_detector = Mock()
        tracker.detect(np.full((721, 961), 235, np.uint8), 1, 0)
        tracker.side_detector.detectMarkers.assert_not_called()


if __name__ == '__main__':
    unittest.main()
