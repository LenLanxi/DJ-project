"""Current-frame identity checks for copies absent from rejected quads.

Synthetic images only; these checks do not measure camera/flight behavior.
"""
import unittest
from unittest.mock import Mock

import cv2
import numpy as np

from r3_follow.vision import MarkerTracker, synthetic_frame


def weak_marker_scene(contrast=8, *, weak_id=0, easy_id=0, include_easy=True):
    """The weak marker is recoverable but default extraction has no quad.

    Keep the background equal to its white cells, avoiding a surrounding
    panel whose outline would accidentally seed the existing ROI rescue.
    """
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    image = np.full((480, 640), 100+contrast, np.uint8)
    marker = cv2.aruco.generateImageMarker(dictionary, weak_id, 120)
    image[180:300, 420:540] = np.where(marker == 0, 100, 100+contrast)
    if include_easy:
        image[180:300, 100:220] = cv2.aruco.generateImageMarker(
            dictionary, easy_id, 120)
    return image


class IdentityVisibilityTests(unittest.TestCase):
    def setUp(self):
        cv2.setNumThreads(1)

    def assert_no_position(self, observation):
        self.assertFalse(observation.found)
        self.assertIsNone(observation.center_x)
        self.assertIsNone(observation.center_y)
        self.assertIsNone(observation.size_ratio)
        self.assertEqual(observation.corners, ())

    def test_weak_fixture_has_no_default_quad_but_is_recoverable(self):
        for contrast in (8, 10, 12):
            with self.subTest(contrast=contrast):
                image = weak_marker_scene(contrast, include_easy=False)
                tracker = MarkerTracker()
                _, ids, rejected = tracker.detector.detectMarkers(image)
                self.assertIsNone(ids)
                self.assertEqual(len(rejected), 0)
                self.assertTrue(tracker.detect(image, 1, 0).found)

    def test_easy_copy_cannot_hide_recoverable_same_id_without_a_quad(self):
        for contrast in (8, 10, 12):
            with self.subTest(contrast=contrast):
                image = weak_marker_scene(contrast)
                before = image.copy()
                tracker = MarkerTracker(processing_budget_ms=120, clock=lambda: 0.)
                _, ids, _ = tracker.detector.detectMarkers(image)
                self.assertEqual(ids.flatten().tolist(), [0])
                observation = tracker.detect(image, 7, 42., 'synthetic_identity')
                self.assert_no_position(observation)
                self.assertEqual(observation.reason, 'ambiguous_duplicate_id')
                self.assertEqual((observation.frame_id, observation.acquired_at,
                                  observation.source), (7, 42., 'synthetic_identity'))
                attempts = tracker.last_diagnostics['attempts']
                identity = [a for a in attempts if a['method'] == 'identity_half_contrast']
                self.assertEqual(len(identity), 1)
                self.assertEqual(identity[0]['ids'], [0, 0])
                self.assertEqual(identity[0]['input_pixels'], 240*320)
                self.assertEqual(tracker.last_diagnostics['duplicate_check']['roi_count'], 0)
                np.testing.assert_array_equal(image, before)

    def test_single_target_remains_unique_and_current(self):
        tracker = MarkerTracker(processing_budget_ms=120, clock=lambda: 0.)
        observation = tracker.detect(synthetic_frame(x=.65, size=.19), 1, 0)
        self.assertTrue(observation.found)
        self.assertEqual(tracker.last_diagnostics['detected_ids'], [0])
        self.assertEqual(tracker.last_diagnostics['duplicate_check']['roi_count'], 0)
        missing = tracker.detect(synthetic_frame(visible=False), 2, .1)
        self.assert_no_position(missing)

    def test_three_selected_ids_keep_their_own_positions(self):
        image = np.full((480, 960), 245, np.uint8)
        dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
        for marker_id in range(3):
            image[190:290, 110+320*marker_id:210+320*marker_id] = (
                cv2.aruco.generateImageMarker(dictionary, marker_id, 100))
        for marker_id in range(3):
            tracker = MarkerTracker(marker_id)
            observation = tracker.detect(image, 1, 0)
            self.assertTrue(observation.found)
            self.assertAlmostEqual(observation.center_x, 159.5+320*marker_id, delta=1)
            self.assertEqual(tracker.last_diagnostics['detected_ids'], [marker_id])
            self.assertEqual(tracker.last_diagnostics['duplicate_check']['roi_count'], 0)

    def test_newly_decoded_other_id_clears_its_pending_quad(self):
        dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
        image = weak_marker_scene(40, weak_id=17, include_easy=False)
        image = cv2.GaussianBlur(image, (0, 0), 5)
        image[180:300, 100:220] = cv2.aruco.generateImageMarker(dictionary, 0, 120)
        tracker = MarkerTracker()
        _, ids, rejected = tracker.detector.detectMarkers(image)
        self.assertEqual(ids.flatten().tolist(), [0])
        self.assertGreater(len(rejected), 0)
        observation = tracker.detect(image, 1, 0)
        self.assertTrue(observation.found)
        self.assertAlmostEqual(observation.center_x, 159.5, delta=1)
        self.assertTrue(any(item['id'] == 17 and item['method'] == 'identity_half_contrast'
                            for item in tracker.last_diagnostics['ignored_other_markers']))
        self.assertEqual(tracker.last_diagnostics['duplicate_check']['candidate_count'], 0)
        self.assertEqual(tracker.last_diagnostics['duplicate_check']['decoder_calls'], 0)

    def test_identity_pass_obeys_existing_deadline(self):
        elapsed = [0.]
        tracker = MarkerTracker(processing_budget_ms=120, clock=lambda: elapsed[0])
        decoder = tracker.motion_detector
        tracker.motion_detector = Mock()
        def delayed(image):
            result = decoder.detectMarkers(image)
            elapsed[0] += .121
            return result
        tracker.motion_detector.detectMarkers.side_effect = delayed
        observation = tracker.detect(synthetic_frame(), 1, 0)
        self.assert_no_position(observation)
        self.assertEqual(observation.reason, 'detection_budget_exceeded')
        self.assertEqual(tracker.last_diagnostics['processing_budget']['exhausted_at'],
                         'identity_half_contrast:after')
        self.assertEqual(tracker.last_diagnostics['processing_budget']['limit_ms'], 120)
        self.assertEqual(len(tracker.last_diagnostics['attempts']), 2)

    def test_decorative_squares_are_all_checked_before_target_is_used(self):
        image = synthetic_frame(x=.65, size=.19)
        for k in range(9):
            cv2.rectangle(image, (20+k*60, 20), (44+k*60, 44), (0, 0, 0), -1)
        tracker = MarkerTracker()
        observation = tracker.detect(image, 1, 0)
        self.assertTrue(observation.found)
        diag = tracker.last_diagnostics['duplicate_check']
        self.assertGreater(diag['roi_count'], 8)
        self.assertEqual(diag['roi_count'], diag['planned_roi_count'])
        self.assertTrue(diag['scan_complete'])
        self.assertEqual(diag['decoder_calls'], 3*diag['roi_count'])

    def test_only_identical_crop_is_removed_before_roi_budget(self):
        image = synthetic_frame(x=.2, size=.12)
        tracker = MarkerTracker()
        corners, ids, _ = tracker.detector.detectMarkers(image)
        # Different contours, identical bounds and scale: the crop pixels
        # supplied to every rescue decoder would otherwise be identical.
        quads = [np.float32([[[400, 100], [480, 100], [480, 180], [400, 180]]]),
                 np.float32([[[400, 100], [480, 100], [480, 180], [440, 180]]])]
        tracker.detector = Mock()
        tracker.detector.detectMarkers.return_value = (corners, ids, quads)
        tracker.DUPLICATE_MAX_ROIS = 1
        observation = tracker.detect(image, 1, 0)
        self.assertTrue(observation.found)
        diag = tracker.last_diagnostics['duplicate_check']
        self.assertEqual(diag['candidate_count'], 2)
        self.assertEqual(diag['planned_roi_count'], 1)
        self.assertEqual(diag['roi_count'], 1)
        self.assertEqual(diag['decoder_calls'], 3)

    def test_overlapping_but_different_crops_still_use_the_budget(self):
        image = synthetic_frame(x=.2, size=.12)
        tracker = MarkerTracker()
        corners, ids, _ = tracker.detector.detectMarkers(image)
        first = np.float32([[[400, 100], [480, 100], [480, 180], [400, 180]]])
        second = first + np.float32([[[6, 0]]])
        tracker.detector = Mock()
        tracker.detector.detectMarkers.return_value = (corners, ids, [first, second])
        tracker.DUPLICATE_MAX_ROIS = 1
        observation = tracker.detect(image, 1, 0)
        self.assert_no_position(observation)
        self.assertEqual(observation.reason, 'ambiguous_scan_budget_exceeded')


if __name__ == '__main__':
    unittest.main()
