"""Bound identity work by pixels/time without losing later target copies.

Real decorative-square input exercises the complete OpenCV path. Controlled
decoder fixtures put a second identity late in the scan and advance only a
fake clock: these tests make no claim about camera or aircraft latency.
"""
import unittest
from unittest.mock import Mock

import cv2
import numpy as np

from follow.r3_follow.vision import MarkerTracker, synthetic_frame


class Clock:
    def __init__(self):
        self.seconds = 0.

    def __call__(self):
        return self.seconds

    def advance_ms(self, milliseconds):
        self.seconds += milliseconds / 1000.


def quad(left, top, side):
    return np.float32([[[left, top], [left + side, top],
                        [left + side, top + side], [left, top + side]]])


class IdentityWorkBudgetTests(unittest.TestCase):
    def setUp(self):
        cv2.setNumThreads(1)

    def assert_no_position(self, observation):
        self.assertFalse(observation.found)
        self.assertIsNone(observation.center_x)
        self.assertIsNone(observation.center_y)
        self.assertIsNone(observation.size_ratio)
        self.assertEqual(observation.corners, ())

    def controlled_tracker(self, candidates, *, image=None, selected=None):
        clock = Clock()
        tracker = MarkerTracker(processing_budget_ms=120, clock=clock)
        if image is None:
            image = synthetic_frame(x=.15, size=.12)
        if selected is None:
            selected, ids, _ = tracker.detector.detectMarkers(image)
            self.assertEqual(ids.flatten().tolist(), [0])
        tracker.detector = Mock()
        tracker.detector.detectMarkers.return_value = (
            selected, np.int32([[0]]), candidates)
        tracker.motion_detector = Mock()
        tracker.motion_detector.detectMarkers.return_value = ([], None, [])
        tracker.side_detector = Mock()
        tracker.side_detector.detectMarkers.return_value = ([], None, [])
        return tracker, clock, image

    def small_candidates(self, count=10):
        # Equal area preserves input order when the scan sorts by area. All
        # crops differ, and none lies inside the actual target at y ~= 240.
        return [quad(20 + 55 * index, 40, 12) for index in range(count)]

    def test_nine_black_squares_complete_all_eighteen_rois_before_return(self):
        image = synthetic_frame(x=.65, size=.19)
        for index in range(9):
            cv2.rectangle(image, (20 + 60 * index, 20),
                          (44 + 60 * index, 44), (0, 0, 0), -1)
        original = image.copy()
        tracker = MarkerTracker(processing_budget_ms=120, clock=Clock())
        observation = tracker.detect(image, 17, 24.5, 'synthetic_background')
        self.assertTrue(observation.found)
        self.assertAlmostEqual(observation.center_x, 415., delta=1)
        self.assertEqual((observation.frame_id, observation.acquired_at,
                          observation.source), (17, 24.5, 'synthetic_background'))
        scan = tracker.last_diagnostics['duplicate_check']
        self.assertEqual(scan['planned_roi_count'], 18)
        self.assertEqual(scan['roi_count'], 18)
        self.assertEqual(scan['decoder_calls'], 54)
        self.assertTrue(scan['scan_complete'])
        self.assertFalse(scan['budget_exceeded'])
        self.assertLessEqual(scan['input_pixels'], 512 * 1024)
        np.testing.assert_array_equal(image, original)

    def test_second_same_id_decoded_in_ninth_roi_rejects_easy_target(self):
        tracker, _, image = self.controlled_tracker(self.small_candidates())
        original = image.copy()
        calls = []

        def late_copy(crop):
            calls.append(crop.shape)
            if len(calls) == 9:
                # The ninth crop begins at (444, 24); local coordinates
                # locate the copy at the ninth candidate (460, 40).
                return [quad(16, 16, 12)], np.int32([[0]]), []
            return [], None, []

        tracker.side_detector.detectMarkers.side_effect = late_copy
        observation = tracker.detect(image, 2, .1)
        self.assert_no_position(observation)
        self.assertEqual(observation.reason, 'ambiguous_duplicate_id')
        self.assertEqual(len(calls), 9)
        scan = tracker.last_diagnostics['duplicate_check']
        self.assertEqual(scan['planned_roi_count'], 10)
        self.assertEqual(scan['roi_count'], 9)
        self.assertEqual(scan['decoder_calls'], 27)
        self.assertFalse(scan['scan_complete'])
        last = tracker.last_diagnostics['attempts'][-1]
        self.assertEqual(last['method'], 'duplicate_roi_edge_exact')
        self.assertEqual(last['image_offset'], [444, 24])
        self.assertEqual(last['ids'], [0])
        np.testing.assert_array_equal(image, original)

    def test_late_other_id_is_not_borrowed_as_target_position(self):
        for other_id in (1, 2):
            with self.subTest(other_id=other_id):
                tracker, _, image = self.controlled_tracker(self.small_candidates())
                result_sequence = [([], None, [])] * 10
                result_sequence[8] = ([quad(16, 16, 12)],
                                      np.int32([[other_id]]), [])
                tracker.side_detector.detectMarkers.side_effect = result_sequence
                observation = tracker.detect(image, 3, .2)
                self.assertTrue(observation.found)
                self.assertEqual(observation.target_id, 0)
                self.assertAlmostEqual(observation.center_x, 95.5, delta=1)
                scan = tracker.last_diagnostics['duplicate_check']
                self.assertEqual(scan['roi_count'], 10)
                self.assertTrue(scan['scan_complete'])
                ignored = tracker.last_diagnostics['ignored_other_markers']
                self.assertTrue(any(item['id'] == other_id and
                                    item['method'] == 'duplicate_roi_edge_exact'
                                    for item in ignored))

    def test_absent_selected_id_does_not_reuse_previous_target_or_other_id(self):
        tracker = MarkerTracker(processing_budget_ms=120, clock=Clock())
        self.assertTrue(tracker.detect(synthetic_frame(marker_id=0), 1, 0).found)
        for frame_id, other_id in ((2, 1), (3, 2)):
            with self.subTest(other_id=other_id):
                image = synthetic_frame(marker_id=other_id)
                original = image.copy()
                observation = tracker.detect(image, frame_id, frame_id / 10.)
                self.assert_no_position(observation)
                self.assertEqual(observation.target_id, 0)
                self.assertEqual(observation.frame_id, frame_id)
                self.assertTrue(any(item['id'] == other_id for item in
                                    tracker.last_diagnostics['ignored_other_markers']))
                np.testing.assert_array_equal(image, original)

    def test_roi_pixel_limit_still_rejects_incomplete_identity_scan(self):
        image = np.full((1200, 1200), 245, np.uint8)
        dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
        image[1000:1080, 850:930] = cv2.aruco.generateImageMarker(dictionary, 0, 80)
        candidates = [quad(150, 150, 300), quad(700, 150, 300), quad(150, 700, 300)]
        tracker, _, image = self.controlled_tracker(
            candidates, image=image, selected=[quad(850, 1000, 79)])
        original = image.copy()
        observation = tracker.detect(image, 4, .3)
        self.assert_no_position(observation)
        self.assertEqual(observation.reason, 'ambiguous_scan_budget_exceeded')
        scan = tracker.last_diagnostics['duplicate_check']
        self.assertEqual(scan['budget_reason'], 'roi_pixels')
        self.assertEqual(scan['planned_roi_count'], 2)
        self.assertEqual(scan['planned_input_pixels'], 512 * 1024)
        self.assertEqual(scan['roi_count'], 0)
        self.assertFalse(scan['scan_complete'])
        tracker.side_detector.detectMarkers.assert_not_called()
        np.testing.assert_array_equal(image, original)

    def test_sixty_fifth_candidate_still_rejects_without_partial_position(self):
        candidates = [quad(15 + 44 * (index % 13), 20 + 30 * (index // 13), 9)
                      for index in range(65)]
        tracker, _, image = self.controlled_tracker(candidates)
        observation = tracker.detect(image, 5, .4)
        self.assert_no_position(observation)
        self.assertEqual(observation.reason, 'ambiguous_scan_budget_exceeded')
        scan = tracker.last_diagnostics['duplicate_check']
        self.assertTrue(scan['candidate_overflow'])
        self.assertEqual(scan['candidate_count'], 64)
        self.assertEqual(scan['budget_reason'], 'candidate_count')
        self.assertEqual(scan['decoder_calls'], 0)
        self.assertFalse(scan['scan_complete'])
        tracker.side_detector.detectMarkers.assert_not_called()

    def test_sixty_four_small_candidates_are_all_checked_before_return(self):
        candidates = [quad(15 + 44 * (index % 13), 20 + 30 * (index // 13), 9)
                      for index in range(64)]
        tracker, _, image = self.controlled_tracker(candidates)
        observation = tracker.detect(image, 7, .6)
        self.assertTrue(observation.found)
        self.assertAlmostEqual(observation.center_x, 95.5, delta=1)
        scan = tracker.last_diagnostics['duplicate_check']
        self.assertEqual(scan['candidate_count'], 64)
        self.assertEqual(scan['roi_count'], 64)
        self.assertEqual(scan['decoder_calls'], 192)
        self.assertTrue(scan['scan_complete'])
        self.assertFalse(scan['budget_exceeded'])
        self.assertEqual(tracker.side_detector.detectMarkers.call_count, 64)

    def test_oversized_input_with_unknown_candidate_still_returns_no_position(self):
        image = np.full((2161, 3840), 245, np.uint8)
        dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
        image[1000:1080, 850:930] = cv2.aruco.generateImageMarker(dictionary, 0, 80)
        tracker, _, image = self.controlled_tracker(
            [quad(150, 150, 12)], image=image, selected=[quad(850, 1000, 79)])
        observation = tracker.detect(image, 8, .7)
        self.assert_no_position(observation)
        self.assertEqual(observation.reason, 'ambiguous_scan_budget_exceeded')
        scan = tracker.last_diagnostics['duplicate_check']
        self.assertEqual(scan['budget_reason'], 'input_pixels')
        self.assertEqual(scan['decoder_calls'], 0)
        self.assertFalse(scan['scan_complete'])
        tracker.side_detector.detectMarkers.assert_not_called()

    def test_deadline_in_ninth_roi_cannot_return_first_target_or_late_copy(self):
        tracker, clock, image = self.controlled_tracker(self.small_candidates())
        calls = []

        def late_overrun(crop):
            calls.append(crop.shape)
            if len(calls) == 9:
                clock.advance_ms(121)
                return [quad(16, 16, 12)], np.int32([[0]]), []
            return [], None, []

        tracker.side_detector.detectMarkers.side_effect = late_overrun
        observation = tracker.detect(image, 6, .5)
        self.assert_no_position(observation)
        self.assertEqual(observation.reason, 'detection_budget_exceeded')
        self.assertEqual(len(calls), 9)
        budget = tracker.last_diagnostics['processing_budget']
        self.assertEqual(budget['exhausted_at'], 'duplicate_roi_edge_exact:after')
        self.assertAlmostEqual(budget['elapsed_ms'], 121)
        self.assertEqual(tracker.last_diagnostics['detected_ids'], [])
        self.assertNotIn('target_quality', tracker.last_diagnostics)


if __name__ == '__main__':
    unittest.main()
